"""The worker: claim a job, run the adapter, record the outcome atomically.

`docs/SPEC.md` §6.2 fixes the sequence. The ordering below is not stylistic — each step is placed
where it is because of what happens when the process dies immediately after it:

1. **claim** — atomic, leased, attempt counted;
2. **stage the object first, outside the outcome transaction.** An object written but never
   referenced is an orphan; a row referencing an object that was never written is a dangling pointer
   to evidence that does not exist. The first is recoverable in principle and the second is not, so
   the upload happens before the commit (`docs/SPEC.md` §4.2, F-028). **No sweep exists yet**: the
   `pending` row records the orphan so one can find it later, and `storage_objects.state='orphaned'`
   is declared in the schema and written by nothing. Reconciliation is not yet delivered, so
   reconciliation;
3. **one transaction** for the observation, the outcome, the event, the object's committed state,
   and the job's completion. Either the result is accepted with all of its evidence, or none of it
   is and the job is retried — never a budget spent with no outcome, nor an outcome with no event
   (F-025, PO-03);
4. **the job completes inside that same transaction.** Marking the job done in a separate commit
   would leave a window where the job is finished but the outcome is not recorded.

Delivery is at least once. A redelivered job that finds its work item already succeeded records
`duplicate_suppressed` rather than a second accepted outcome — the partial unique index is what
makes that safe, and this code is what makes it graceful (PO-02).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import Connection, Engine, and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from labbridge.analysis import lsv
from labbridge.domain.candidates import HerCandidate
from labbridge.domain.provenance import Provenance, SourceRecord, SyntheticRoot
from labbridge.domain.quantities import UNKNOWN_UNIT
from labbridge.domain.results import QuantityDescriptor, metric_id, observation_id
from labbridge.environments.her_replay import (
    AdapterSuccess,
    AdapterUnavailable,
    HerReplayAdapter,
    UnsupportedSchemaError,
)
from labbridge.infrastructure.objectstore import ObjectStore
from labbridge.infrastructure.persistence.tables import (
    attempt_outcomes,
    attempts,
    budget_ledger,
    campaigns,
    derived_metrics,
    events,
    observations,
    storage_objects,
    work_items,
)
from labbridge.runtime import jobs
from labbridge.runtime.events import append_event, current_sequence

WORKER_VERSION: Final = "1"
#: Recorded on the synthetic lineage root so a fixture-backed result names its generator.
FIXTURE_GENERATOR: Final = "labbridge.infrastructure.her_ingestion.fixture"
#: The descriptors an LSV observation carries. Ordered: axis order is part of the content identity.
LSV_DESCRIPTORS: Final = (
    QuantityDescriptor(name="potential_vs_rhe", unit="V", axis=0),
    QuantityDescriptor(name="current_density", unit="A/cm^2", axis=1),
    QuantityDescriptor(name="current_density_standard_deviation", unit="A/cm^2", axis=2),
)


@dataclass(frozen=True)
class WorkOutcome:
    """What one turn of the worker did, returned so a caller can assert on it not on logs."""

    job_id: uuid.UUID
    status: str
    observation_id: str | None = None
    failure_code: str | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _object_key(campaign_id: uuid.UUID, digest: str) -> str:
    """Content-addressed within a campaign, so a retry of the same bytes writes the same key."""
    return f"observations/{campaign_id}/{digest}"


def _already_succeeded(connection: Connection, work_item_id: uuid.UUID) -> bool:
    return (
        connection.execute(
            select(func.count())
            .select_from(attempt_outcomes)
            .where(
                and_(
                    attempt_outcomes.c.work_item_id == work_item_id,
                    attempt_outcomes.c.status == "succeeded",
                )
            )
        ).scalar_one()
        > 0
    )


def _next_ordinal(connection: Connection, work_item_id: uuid.UUID) -> int:
    highest = connection.execute(
        select(func.max(attempts.c.ordinal)).where(attempts.c.work_item_id == work_item_id)
    ).scalar_one()
    return int(highest or 0) + 1


def _campaign_of(connection: Connection, work_item_id: uuid.UUID) -> tuple[uuid.UUID, str, str]:
    row = connection.execute(
        select(campaigns.c.campaign_id, campaigns.c.data_origin, campaigns.c.execution_mode)
        .select_from(work_items.join(campaigns))
        .where(work_items.c.work_item_id == work_item_id)
    ).one()
    return row.campaign_id, row.data_origin, row.execution_mode


def _record_cost(
    connection: Connection, campaign_id: uuid.UUID, work_item_id: uuid.UUID, kind: str
) -> None:
    """Append one budget entry, inside the outcome's transaction.

    Append-only: a reservation and its release are two rows, never an update of one, so the ledger
    reconstructs how a budget was spent rather than only its current total (`docs/SPEC.md` §8).

    The unit is `attempt`, not money. A replay costs no consumables and no compute worth pricing;
    counting attempts is the honest measure of what this environment actually spends, and inventing
    a currency amount would put a number in the ledger that means nothing.
    """
    connection.execute(
        budget_ledger.insert().values(
            entry_id=uuid.uuid4(),
            campaign_id=campaign_id,
            work_item_id=work_item_id,
            kind=kind,
            amount=1,
            unit="attempt",
            reason="one replay attempt against the HER environment",
            recorded_at=func.now(),
        )
    )


def _candidate_of(connection: Connection, work_item_id: uuid.UUID) -> HerCandidate:
    payload = connection.execute(
        select(work_items.c.candidate).where(work_items.c.work_item_id == work_item_id)
    ).scalar_one()
    return HerCandidate.model_validate(payload)


def _latest_attempt_event(connection: Connection, attempt_id: uuid.UUID) -> uuid.UUID:
    event_id = connection.execute(
        select(events.c.event_id)
        .where(events.c.aggregate_type == "attempt", events.c.aggregate_id == attempt_id)
        .order_by(events.c.sequence.desc())
        .limit(1)
    ).scalar_one()
    return uuid.UUID(str(event_id))


def _append_attempt_completed(
    connection: Connection,
    *,
    lease: jobs.Lease,
    attempt_id: uuid.UUID,
    campaign_id: uuid.UUID,
) -> uuid.UUID:
    row = connection.execute(
        select(
            attempts.c.state,
            attempts.c.started_at,
            attempts.c.created_at,
            attempt_outcomes.c.status,
            attempt_outcomes.c.observation_id,
            attempt_outcomes.c.failure,
            attempt_outcomes.c.cost,
            attempt_outcomes.c.data_origin,
            attempt_outcomes.c.execution_mode,
            attempt_outcomes.c.provenance,
            attempt_outcomes.c.finished_at,
        )
        .select_from(attempts.join(attempt_outcomes))
        .where(attempts.c.attempt_id == attempt_id)
    ).one()
    correlation_id, _ = jobs.event_context(connection, lease.job_id)
    appended = append_event(
        connection,
        campaign_id=campaign_id,
        aggregate_id=attempt_id,
        aggregate_type="attempt",
        event_type="attempt.completed",
        payload={
            "work_item_id": lease.work_item_id,
            "campaign_id": campaign_id,
            "state": row.state,
            "status": row.status,
            "observation_id": row.observation_id,
            "failure": row.failure,
            "cost": row.cost,
            "data_origin": row.data_origin,
            "execution_mode": row.execution_mode,
            "provenance": row.provenance,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
        },
        expected_version=current_sequence(
            connection,
            campaign_id=campaign_id,
            aggregate_type="attempt",
            aggregate_id=attempt_id,
        ),
        correlation_id=correlation_id,
        causation_id=_latest_attempt_event(connection, attempt_id),
    )
    return appended.event_id


def _append_work_item_state(
    connection: Connection,
    *,
    lease: jobs.Lease,
    campaign_id: uuid.UUID,
    state: str,
    causation_id: uuid.UUID,
    reason: str | None = None,
) -> uuid.UUID:
    correlation_id, _ = jobs.event_context(connection, lease.job_id)
    appended = append_event(
        connection,
        campaign_id=campaign_id,
        aggregate_id=lease.work_item_id,
        aggregate_type="work_item",
        event_type=f"work_item.{state}",
        payload={"state": state, "reason": reason},
        expected_version=current_sequence(
            connection,
            campaign_id=campaign_id,
            aggregate_type="work_item",
            aggregate_id=lease.work_item_id,
        ),
        correlation_id=correlation_id,
        causation_id=causation_id,
    )
    return appended.event_id


class Worker:
    """One worker process. Owns no state between turns beyond its name."""

    def __init__(
        self,
        engine: Engine,
        adapter: HerReplayAdapter,
        store: ObjectStore,
        *,
        name: str,
        fixture_seed: int = 0,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._engine = engine
        self._adapter = adapter
        self._store = store
        self.name = name
        self._fixture_seed = fixture_seed
        self._clock = clock

    async def run_once(self) -> WorkOutcome | None:  # noqa: PLR0911
        """Claim and process one job, or return None when the queue is empty.

        The claim commits on its own so the lease is visible to every other worker immediately. A
        claim held open until the work finished would serialise the whole pool behind one job.

        Many exits, on purpose: empty queue, environment mismatch, unsupported schema, any other
        adapter error, unavailable location, lease lost, outcome-write failure, success. Each one
        writes a different record, and collapsing them would be collapsing the distinctions the
        evidence depends on.
        """
        with self._engine.begin() as connection:
            lease = jobs.claim(connection, owner=self.name)
            if lease is None:
                return None
            jobs.mark_running(connection, lease)
            campaign_id, origin, mode = _campaign_of(connection, lease.work_item_id)
            candidate = _candidate_of(connection, lease.work_item_id)
            mismatch = self._environment_mismatch(origin, mode)
            ordinal = _next_ordinal(connection, lease.work_item_id)
            attempt_id = uuid.uuid4()
            attempt_row = connection.execute(
                attempts.insert()
                .values(
                    attempt_id=attempt_id,
                    work_item_id=lease.work_item_id,
                    job_id=lease.job_id,
                    ordinal=ordinal,
                    state="running",
                    started_at=func.now(),
                    created_at=func.now(),
                )
                .returning(attempts.c.started_at, attempts.c.created_at)
            ).one()
            correlation_id, job_event_id = jobs.event_context(connection, lease.job_id)
            append_event(
                connection,
                campaign_id=campaign_id,
                aggregate_id=attempt_id,
                aggregate_type="attempt",
                event_type="attempt.started",
                payload={
                    "work_item_id": lease.work_item_id,
                    "job_id": lease.job_id,
                    "ordinal": ordinal,
                    "state": "running",
                    "started_at": attempt_row.started_at,
                    "created_at": attempt_row.created_at,
                },
                expected_version=0,
                correlation_id=correlation_id,
                causation_id=job_event_id,
            )

        if mismatch is not None:
            # The campaign declared an origin and mode the adapter cannot serve. Writing the
            # campaign's label onto bytes the adapter says are something else is the conflation
            # invariant 1 forbids: an `observed` row whose lineage resolves to a synthetic root,
            # and a bundle manifest that then declares the whole campaign observed. Refuse instead.
            # Recorded under the *campaign's* pair, which is what the composite foreign key
            # requires and what is honest here: this outcome produced no bytes, so its origin
            # column describes the campaign's declared environment rather than any data. The
            # adapter's pair is named in the summary. What matters is that no observation and no
            # metric is written at all — the refusal happens before anything is read.
            return self._record_failure(
                lease,
                attempt_id,
                campaign_id,
                origin,
                mode,
                "environment_mismatch",
                mismatch,
                retryable=False,
                category="policy",
            )

        # From here the columns are the *adapter's* pair, not the campaign's. They are equal — the
        # check above guarantees it — but taking them from the adapter says which one is the truth
        # about the bytes.
        origin = self._adapter.environment.data_origin
        mode = self._adapter.environment.execution_mode

        try:
            result = await self._adapter.execute(candidate)
        except UnsupportedSchemaError as error:
            # F-019: an unrecognised schema will not parse on the next attempt either.
            return self._record_failure(
                lease,
                attempt_id,
                campaign_id,
                origin,
                mode,
                error.code,
                str(error),
                retryable=False,
            )
        except Exception as error:
            # Every other failure. Catching broadly is deliberate: an exception that escapes here
            # leaves the attempt `running`, the job leased by a process that is about to move on,
            # and no record of why — which is the evidence loss invariant 2 forbids. The failure is
            # classified retryable because a transport or storage fault usually is; a terminal one
            # is recognised above by its type, not guessed at from a message.
            return self._record_failure(
                lease,
                attempt_id,
                campaign_id,
                origin,
                mode,
                "adapter_error",
                f"{type(error).__name__}: {error}",
                retryable=True,
                category="transport",
                exception_type=type(error).__name__,
            )

        if isinstance(result, AdapterUnavailable):
            # F-017: the location was never measured. Terminal, not retryable — trying again will
            # not make an unmeasured location measured.
            return self._record_failure(
                lease,
                attempt_id,
                campaign_id,
                origin,
                mode,
                result.failure_code,
                result.reason,
                retryable=False,
                # Not `instrument`: the archive omits 20 areas per library from SECCM by design,
                # so nothing failed. Recording an instrument fault would assert a breakage the
                # source records as a deliberate exclusion (F-017).
                category="policy",
            )

        try:
            return self._record_success(lease, attempt_id, campaign_id, origin, mode, result)
        except jobs.LeaseLostError:
            # The lease lapsed while the adapter ran and another worker owns the job now. The
            # result is correctly rejected — but the rejection must leave a record, or the attempt
            # vanishes from the campaign's history (F-008). Written on its own connection, because
            # the transaction that failed has already rolled back.
            return self._record_lease_lost(lease, attempt_id, campaign_id, origin, mode)
        except Exception as error:
            return self._record_failure(
                lease,
                attempt_id,
                campaign_id,
                origin,
                mode,
                "outcome_write_failed",
                f"{type(error).__name__}: {error}",
                retryable=True,
                exception_type=type(error).__name__,
            )

    def _environment_mismatch(self, origin: str, mode: str) -> str | None:
        """Whether the campaign's declared pair disagrees with what the adapter is actually reading.

        The campaign row is a client's declaration; the adapter's pair came from the evidence on
        disk (ADR-010). When they differ the declaration is wrong, and the only safe answer is to
        refuse: relabelling the bytes to match the declaration is the origin conflation, and
        relabelling the campaign to match the adapter would rewrite a client's intent.
        """
        environment = self._adapter.environment
        if (origin, mode) == (environment.data_origin, environment.execution_mode):
            return None
        return (
            f"campaign declares {origin}+{mode} but the adapter reads "
            f"{environment.data_origin}+{environment.execution_mode} from its source root"
        )

    def _provenance(self, result: AdapterSuccess | None) -> Provenance:
        """The lineage root, chosen by what the adapter is actually reading.

        `data_origin` comes from the adapter's environment, which came from the evidence on disk
        (ADR-010). Nothing here decides it, which is why a fixture-backed run cannot be recorded as
        observed however the campaign row is configured.

        A failure that read nothing has no root, and `Provenance` now allows that: there is no
        member to cite and no generated configuration to name, so inventing either would put a
        false path in the record. The root is required where PO-06 actually requires it — on an
        observation and on a derived metric — and both validate it.
        """
        environment = self._adapter.environment
        source: SourceRecord | None = None
        synthetic: SyntheticRoot | None = None
        if result is not None:
            if environment.data_origin == "observed":
                source = self._adapter.source_record_for(result.source_path)
            else:
                synthetic = self._adapter.synthetic_root()
        return Provenance(
            environment=environment,
            source_record=source,
            synthetic_root=synthetic,
            code_version=WORKER_VERSION,
            config_hash=self._adapter.environment.adapter_version,
        )

    def _record_success(
        self,
        lease: jobs.Lease,
        attempt_id: uuid.UUID,
        campaign_id: uuid.UUID,
        origin: str,
        mode: str,
        result: AdapterSuccess,
    ) -> WorkOutcome:
        provenance = self._provenance(result)
        identity = observation_id(
            sha256=result.source_sha256,
            schema_version=result.schema_version,
            signal_kind=result.signal_kind,
            quantities=LSV_DESCRIPTORS,
            provenance=provenance,
        )

        # Step 2: the object goes first, outside the transaction. An orphan is recoverable; a row
        # pointing at bytes that were never written is not.
        key = _object_key(campaign_id, result.source_sha256)
        uri = f"s3://{self._store.bucket}/{key}"
        with self._engine.begin() as connection:
            # `pending` first, in its own transaction: if the process dies during the upload, this
            # row is the only record that the object exists, and is what a future sweep would need.
            # Nothing sweeps today. `DO NOTHING` because a retry of the same bytes writes the same
            # content-addressed key, and that is a no-op rather than a clash.
            connection.execute(
                pg_insert(storage_objects)
                .values(
                    object_uri=uri,
                    bucket=self._store.bucket,
                    object_key=key,
                    state="pending",
                    created_at=func.now(),
                )
                .on_conflict_do_nothing(index_elements=["object_uri"])
            )
        stored = self._store.put_and_verify(key, result.payload, media_type=result.media_type)

        # Step 3 and 4: one transaction for the evidence, the event, and the job.
        with self._engine.begin() as connection:
            if _already_succeeded(connection, lease.work_item_id):
                self._write_outcome(
                    connection,
                    lease,
                    attempt_id,
                    campaign_id,
                    origin,
                    mode,
                    status="duplicate_suppressed",
                    provenance=provenance,
                )
                connection.execute(
                    attempts.update()
                    .where(attempts.c.attempt_id == attempt_id)
                    .values(state="cancelled")
                )
                completed_event_id = _append_attempt_completed(
                    connection,
                    lease=lease,
                    attempt_id=attempt_id,
                    campaign_id=campaign_id,
                )
                jobs.complete(connection, lease, causation_id=completed_event_id)
                return WorkOutcome(lease.job_id, "duplicate_suppressed")

            observation_row = connection.execute(
                observations.insert()
                .values(
                    observation_id=identity,
                    campaign_id=campaign_id,
                    work_item_id=lease.work_item_id,
                    attempt_id=attempt_id,
                    media_type=result.media_type,
                    object_uri=stored.uri,
                    byte_size=stored.byte_size,
                    sha256=stored.sha256,
                    schema_version=result.schema_version,
                    signal_kind=result.signal_kind,
                    quantities=[d.model_dump(mode="json") for d in LSV_DESCRIPTORS],
                    status="accepted",
                    data_origin=origin,
                    execution_mode=mode,
                    provenance=provenance.model_dump(mode="json"),
                    received_at=func.now(),
                )
                .returning(observations.c.received_at)
            ).one()
            correlation_id, _ = jobs.event_context(connection, lease.job_id)
            append_event(
                connection,
                campaign_id=campaign_id,
                aggregate_id=attempt_id,
                aggregate_type="attempt",
                event_type="observation.accepted",
                payload={
                    "observation_id": identity,
                    "work_item_id": lease.work_item_id,
                    "attempt_id": attempt_id,
                    "media_type": result.media_type,
                    "object_uri": stored.uri,
                    "byte_size": stored.byte_size,
                    "sha256": stored.sha256,
                    "schema_version": result.schema_version,
                    "signal_kind": result.signal_kind,
                    "quantities": [d.model_dump(mode="json") for d in LSV_DESCRIPTORS],
                    "status": "accepted",
                    "status_reason": None,
                    "data_origin": origin,
                    "execution_mode": mode,
                    "provenance": provenance.model_dump(mode="json"),
                    "received_at": observation_row.received_at,
                },
                expected_version=1,
                correlation_id=correlation_id,
                causation_id=_latest_attempt_event(connection, attempt_id),
            )
            connection.execute(
                storage_objects.update()
                .where(storage_objects.c.object_uri == stored.uri)
                .values(
                    state="committed",
                    sha256=stored.sha256,
                    byte_size=stored.byte_size,
                    committed_at=func.now(),
                )
            )
            self._write_outcome(
                connection,
                lease,
                attempt_id,
                campaign_id,
                origin,
                mode,
                status="succeeded",
                provenance=provenance,
                observation=identity,
            )
            self._write_metrics(connection, identity, attempt_id, result, provenance)
            _record_cost(connection, campaign_id, lease.work_item_id, "consumed")
            connection.execute(
                attempts.update()
                .where(attempts.c.attempt_id == attempt_id)
                .values(state="succeeded")
            )
            completed_event_id = _append_attempt_completed(
                connection,
                lease=lease,
                attempt_id=attempt_id,
                campaign_id=campaign_id,
            )
            item_update = connection.execute(
                work_items.update()
                .where(
                    and_(
                        work_items.c.work_item_id == lease.work_item_id,
                        work_items.c.state.in_(("queued", "quarantined")),
                    )
                )
                .values(state="accepted", updated_at=func.now())
            )
            causal_event_id = completed_event_id
            if item_update.rowcount == 1:
                causal_event_id = _append_work_item_state(
                    connection,
                    lease=lease,
                    campaign_id=campaign_id,
                    state="accepted",
                    causation_id=completed_event_id,
                )
            jobs.complete(connection, lease, causation_id=causal_event_id)

        return WorkOutcome(lease.job_id, "succeeded", observation_id=identity)

    def _record_failure(
        self,
        lease: jobs.Lease,
        attempt_id: uuid.UUID,
        campaign_id: uuid.UUID,
        origin: str,
        mode: str,
        failure_code: str,
        summary: str,
        *,
        retryable: bool,
        category: str = "instrument",
        exception_type: str | None = None,
    ) -> WorkOutcome:
        """Record why an attempt did not produce an observation.

        `category` is passed rather than hardcoded: an excluded location is not an instrument
        fault. The archive omits 20 areas per library from SECCM by design, and recording that as
        `instrument` would assert the instrument failed where the source records that no
        measurement was attempted (F-017).
        """
        status = "failed_retryable" if retryable else "failed_terminal"
        provenance = self._provenance(None)
        with self._engine.begin() as connection:
            self._write_outcome(
                connection,
                lease,
                attempt_id,
                campaign_id,
                origin,
                mode,
                status=status,
                provenance=provenance,
                failure={
                    "failure_code": failure_code,
                    "category": category,
                    "retryable": retryable,
                    "summary": summary,
                    "exception_type": exception_type,
                },
            )
            connection.execute(
                attempts.update().where(attempts.c.attempt_id == attempt_id).values(state=status)
            )
            completed_event_id = _append_attempt_completed(
                connection,
                lease=lease,
                attempt_id=attempt_id,
                campaign_id=campaign_id,
            )
            causal_event_id = completed_event_id
            if not retryable:
                # Guarded on the source state. An unconditional UPDATE would drive an already
                # `accepted` item to `rejected` on a redelivery whose adapter now reports the
                # location unavailable, and the projection would then contradict the observation
                # it points at. `accepted` and `rejected` are terminal (docs/SPEC.md §7.2).
                item_update = connection.execute(
                    work_items.update()
                    .where(
                        and_(
                            work_items.c.work_item_id == lease.work_item_id,
                            work_items.c.state.in_(("queued", "quarantined")),
                        )
                    )
                    .values(state="rejected", updated_at=func.now())
                )
                if item_update.rowcount == 1:
                    causal_event_id = _append_work_item_state(
                        connection,
                        lease=lease,
                        campaign_id=campaign_id,
                        state="rejected",
                        causation_id=completed_event_id,
                        reason=summary,
                    )
            # A failed attempt still consumed one, so the ledger records it. A budget that only
            # counted successes would let a campaign burn its allowance invisibly.
            _record_cost(connection, campaign_id, lease.work_item_id, "consumed")
            if retryable:
                # Backoff applied by the database, and the attempt cap enforced there too, so a
                # failing dependency is retried without becoming a retry storm.
                jobs.schedule_retry(
                    connection,
                    lease,
                    failure={"failure_code": failure_code},
                    causation_id=causal_event_id,
                )
            else:
                jobs.fail_terminally(
                    connection,
                    lease,
                    failure={"failure_code": failure_code},
                    causation_id=causal_event_id,
                )
        return WorkOutcome(lease.job_id, status, failure_code=failure_code)

    def _record_lease_lost(
        self,
        lease: jobs.Lease,
        attempt_id: uuid.UUID,
        campaign_id: uuid.UUID,
        origin: str,
        mode: str,
    ) -> WorkOutcome:
        """F-008. The lease lapsed while the adapter ran, so this result is rejected — but the
        rejection has to leave a record, or the attempt disappears from the campaign's history.

        The job is deliberately not touched: it belongs to another worker now, and this one has no
        standing to change its state.
        """
        provenance = self._provenance(None)
        with self._engine.begin() as connection:
            self._write_outcome(
                connection,
                lease,
                attempt_id,
                campaign_id,
                origin,
                mode,
                status="lease_lost",
                provenance=provenance,
                failure={
                    "failure_code": "lease_lost",
                    "category": "worker",
                    "retryable": False,
                    "summary": "the lease expired during the adapter call and another worker "
                    "claimed the job; this result was not accepted",
                    "exception_type": None,
                },
            )
            connection.execute(
                attempts.update()
                .where(attempts.c.attempt_id == attempt_id)
                .values(state="lease_lost")
            )
            _record_cost(connection, campaign_id, lease.work_item_id, "consumed")
            _append_attempt_completed(
                connection,
                lease=lease,
                attempt_id=attempt_id,
                campaign_id=campaign_id,
            )
        return WorkOutcome(lease.job_id, "lease_lost", failure_code="lease_lost")

    def _write_metrics(
        self,
        connection: Connection,
        identity: str,
        attempt_id: uuid.UUID,
        result: AdapterSuccess,
        provenance: Provenance,
    ) -> None:
        """Derive and record the metric, in the same transaction as the observation it came from.

        A rejected metric is still written. `F-021` is explicit that an analysis failing does not
        invalidate the acquisition: the observation stays accepted and retained, and the metric
        carries the reason it could not be computed. Dropping the row instead would leave no record
        that the analysis was ever attempted.
        """
        analysis = lsv.analyse(
            result.payload,
            potential_unit=LSV_DESCRIPTORS[0].unit,
            current_unit=LSV_DESCRIPTORS[1].unit,
        )
        parameters = lsv.parameter_hash()
        for name, quantity in (
            (lsv.METRIC_CURRENT, analysis.current_extremum),
            (lsv.METRIC_POTENTIAL, analysis.potential_at_extremum),
        ):
            # A rejected analysis produces no value, and the row is written anyway with a null
            # value and the reason. Skipping it left no record that the analysis had run at all,
            # so a bundle reader could not tell "rejected, here is why" from "never attempted" —
            # which is the misrepresentation this module's own docstring says it prevents (F-021).
            connection.execute(
                derived_metrics.insert().values(
                    metric_id=metric_id(
                        observation_id=identity,
                        attempt_id=str(attempt_id),
                        name=name,
                        analysis_name=lsv.ANALYSIS_NAME,
                        analysis_version=lsv.ANALYSIS_VERSION,
                        parameter_hash=parameters,
                    ),
                    observation_id=identity,
                    attempt_id=attempt_id,
                    name=name,
                    value_numeric=quantity.value if quantity else None,
                    value=quantity.model_dump(mode="json") if quantity else {},
                    unit=quantity.unit if quantity else UNKNOWN_UNIT,
                    # The area the current density is normalised by. Carried onto the row and into
                    # the bundle: a caveat reachable only by joining back to the observation is a
                    # caveat a chart-builder will not join for.
                    normalisation_basis=analysis.area_basis,
                    analysis_name=lsv.ANALYSIS_NAME,
                    analysis_version=lsv.ANALYSIS_VERSION,
                    parameter_hash=parameters,
                    quality_status=analysis.quality_status,
                    quality_reason=analysis.quality_reason,
                    provenance=provenance.model_dump(mode="json"),
                    created_at=func.now(),
                )
            )

    def _write_outcome(
        self,
        connection: Connection,
        lease: jobs.Lease,
        attempt_id: uuid.UUID,
        campaign_id: uuid.UUID,
        origin: str,
        mode: str,
        *,
        status: str,
        provenance: Provenance,
        observation: str | None = None,
        failure: dict[str, object] | None = None,
    ) -> None:
        connection.execute(
            attempt_outcomes.insert().values(
                attempt_id=attempt_id,
                work_item_id=lease.work_item_id,
                campaign_id=campaign_id,
                status=status,
                observation_id=observation,
                failure=failure,
                cost={},
                data_origin=origin,
                execution_mode=mode,
                provenance=provenance.model_dump(mode="json"),
                started_at=func.now(),
                finished_at=func.now(),
            )
        )

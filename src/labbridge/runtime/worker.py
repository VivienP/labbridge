"""The worker: claim a job, run the adapter, record the outcome atomically.

`docs/SPEC.md` §6.2 fixes the sequence. The ordering below is not stylistic — each step is placed
where it is because of what happens when the process dies immediately after it:

1. **claim** — atomic, leased, attempt counted;
2. **stage the object first, outside the outcome transaction.** An object written but never
   referenced is an orphan, which a sweep can find and which costs storage. A row referencing an
   object that was never written is a dangling pointer to evidence that does not exist, and no sweep
   can repair it. Orphans are the cheaper failure, so the upload happens before the commit
   (`docs/SPEC.md` §4.2, F-028);
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
    campaigns,
    derived_metrics,
    observations,
    storage_objects,
    work_items,
)
from labbridge.runtime import jobs
from labbridge.runtime.events import append_event

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


def _candidate_of(connection: Connection, work_item_id: uuid.UUID) -> HerCandidate:
    payload = connection.execute(
        select(work_items.c.candidate).where(work_items.c.work_item_id == work_item_id)
    ).scalar_one()
    return HerCandidate.model_validate(payload)


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

    async def run_once(self) -> WorkOutcome | None:
        """Claim and process one job, or return None when the queue is empty.

        The claim commits on its own so the lease is visible to every other worker immediately. A
        claim held open until the work finished would serialise the whole pool behind one job.
        """
        with self._engine.begin() as connection:
            lease = jobs.claim(connection, owner=self.name)
            if lease is None:
                return None
            jobs.mark_running(connection, lease)
            campaign_id, origin, mode = _campaign_of(connection, lease.work_item_id)
            candidate = _candidate_of(connection, lease.work_item_id)
            ordinal = _next_ordinal(connection, lease.work_item_id)
            attempt_id = uuid.uuid4()
            connection.execute(
                attempts.insert().values(
                    attempt_id=attempt_id,
                    work_item_id=lease.work_item_id,
                    job_id=lease.job_id,
                    ordinal=ordinal,
                    state="running",
                    started_at=func.now(),
                    created_at=func.now(),
                )
            )

        try:
            result = await self._adapter.execute(candidate)
        except UnsupportedSchemaError as error:
            return self._record_terminal_failure(
                lease, attempt_id, campaign_id, origin, mode, error.code, str(error)
            )

        if isinstance(result, AdapterUnavailable):
            # F-017: the location was never measured. Terminal, not retryable — trying again will
            # not make an unmeasured location measured.
            return self._record_terminal_failure(
                lease, attempt_id, campaign_id, origin, mode, result.failure_code, result.reason
            )

        return self._record_success(lease, attempt_id, campaign_id, origin, mode, result)

    def _provenance(self, result: AdapterSuccess | None) -> Provenance:
        """The lineage root, chosen by what the adapter is actually reading.

        `data_origin` comes from the adapter's environment, which came from the evidence on disk
        (ADR-010). Nothing here decides it, which is why a fixture-backed run cannot be recorded as
        observed however the campaign row is configured.

        A failure carries no `AdapterSuccess`, so an observed-origin failure has no member path to
        cite. Provenance is still required — §3.5 makes it mandatory on every outcome — so the
        source record names the archive without a member, which is exactly what is known.
        """
        environment = self._adapter.environment
        source: SourceRecord | None = None
        synthetic: SyntheticRoot | None = None
        if environment.data_origin == "observed":
            source = self._adapter.source_record_for(
                result.source_path if result is not None else None
            )
        else:
            synthetic = self._adapter.synthetic_root(
                generator=FIXTURE_GENERATOR,
                generator_version=self._adapter.environment.adapter_version,
                seed=self._fixture_seed,
            )
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
            # row is what lets a sweep find the orphan. `DO NOTHING` because a retry of the same
            # bytes writes the same content-addressed key, and that is a no-op rather than a clash.
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
                jobs.complete(connection, lease)
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
                return WorkOutcome(lease.job_id, "duplicate_suppressed")

            connection.execute(
                observations.insert().values(
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
            connection.execute(
                attempts.update()
                .where(attempts.c.attempt_id == attempt_id)
                .values(state="succeeded")
            )
            connection.execute(
                work_items.update()
                .where(work_items.c.work_item_id == lease.work_item_id)
                .values(state="accepted", updated_at=func.now())
            )
            append_event(
                connection,
                campaign_id=campaign_id,
                aggregate_id=lease.work_item_id,
                aggregate_type="work_item",
                event_type="observation.accepted",
                payload={"observation_id": identity, "attempt_id": str(attempt_id)},
            )
            jobs.complete(connection, lease)

        return WorkOutcome(lease.job_id, "succeeded", observation_id=identity)

    def _record_terminal_failure(
        self,
        lease: jobs.Lease,
        attempt_id: uuid.UUID,
        campaign_id: uuid.UUID,
        origin: str,
        mode: str,
        failure_code: str,
        summary: str,
    ) -> WorkOutcome:
        provenance = self._provenance(None)
        with self._engine.begin() as connection:
            self._write_outcome(
                connection,
                lease,
                attempt_id,
                campaign_id,
                origin,
                mode,
                status="failed_terminal",
                provenance=provenance,
                failure={
                    "failure_code": failure_code,
                    "category": "instrument",
                    "retryable": False,
                    "summary": summary,
                },
            )
            connection.execute(
                attempts.update()
                .where(attempts.c.attempt_id == attempt_id)
                .values(state="failed_terminal")
            )
            connection.execute(
                work_items.update()
                .where(work_items.c.work_item_id == lease.work_item_id)
                .values(state="rejected", updated_at=func.now())
            )
            append_event(
                connection,
                campaign_id=campaign_id,
                aggregate_id=lease.work_item_id,
                aggregate_type="work_item",
                event_type="attempt.failed_terminal",
                payload={"failure_code": failure_code, "attempt_id": str(attempt_id)},
            )
            jobs.fail_terminally(connection, lease, failure={"failure_code": failure_code})
        return WorkOutcome(lease.job_id, "failed_terminal", failure_code=failure_code)

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
            if quantity is None:
                continue
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
                    value_numeric=quantity.value,
                    value=quantity.model_dump(mode="json"),
                    unit=quantity.unit,
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

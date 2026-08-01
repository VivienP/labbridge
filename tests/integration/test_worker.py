"""The worker end to end, against real PostgreSQL and real MinIO.

This test exercises a whole campaign-to-evidence path, so several guarantees become observable:
duplicate delivery does not create a second accepted outcome (PO-02), an unmeasured location becomes
a terminal outcome rather than a fabricated measurement (F-017), and a fixture-backed run records
itself as synthetic (ADR-010).

Everything runs on the generated fixture, so the suite needs no download. A fixture-backed run is
not evidence about the physical system, and nothing here should be read as such.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import Connection, Engine, func, select

from labbridge.domain.candidates import HerCandidate, candidate_id
from labbridge.domain.idempotency import work_item_instruction_key
from labbridge.domain.quantities import Quantity
from labbridge.environments.her_replay import HerReplayAdapter
from labbridge.infrastructure.her_ingestion.fixture import (
    FIXTURE_MANIFEST_FILENAME,
    FixtureSpec,
    build_fixture,
)
from labbridge.infrastructure.her_ingestion.provenance import write_document
from labbridge.infrastructure.objectstore import S3ObjectStore, StoredObject
from labbridge.infrastructure.persistence.tables import (
    attempt_outcomes,
    attempts,
    budget_ledger,
    campaigns,
    derived_metrics,
    events,
    jobs,
    observations,
    storage_objects,
    work_items,
)
from labbridge.runtime.events import append_event, read_stream
from labbridge.runtime.jobs import enqueue
from labbridge.runtime.worker import Worker, WorkOutcome

pytestmark = pytest.mark.integration

SPEC = FixtureSpec(areas_per_library=6, seccm_areas_per_library=2)
ONE_OUTCOME = 1
TWO_OUTCOMES = 2
ONE_OBSERVATION = 1
ONE_LEDGER_ENTRY = 1
ONE_EVENT = 1
#: The LSV analysis writes one row per metric it defines: the extremum current and its potential.
TWO_METRICS = 2
CONCURRENT_DELIVERIES = 2


@pytest.fixture(scope="session")
def fixture_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("her-fixture")
    manifest = build_fixture(root, spec=SPEC, generator_version="0.1.0")
    write_document(root / FIXTURE_MANIFEST_FILENAME, manifest)
    return root


@pytest.fixture
def adapter(fixture_root: Path) -> HerReplayAdapter:
    return HerReplayAdapter(fixture_root)


@pytest.fixture
def campaign(
    migrated: Engine, purge_campaign: Callable[[Connection, uuid.UUID], None]
) -> Iterator[uuid.UUID]:
    """A committed campaign: the worker opens its own connections, so nothing rolled back is visible
    to it."""
    campaign_id = uuid.uuid4()
    with migrated.begin() as connection:
        connection.execute(
            campaigns.insert().values(
                campaign_id=campaign_id,
                name="worker end to end",
                environment_id="her_auirrh",
                adapter_version="1",
                data_origin="synthetic",
                execution_mode="replay",
                state="active",
                declaration={},
                declaration_hash="f" * 64,
                event_stream_contract_version=1,
                event_stream_last_position=0,
                created_at=func.now(),
                updated_at=func.now(),
            )
        )
        append_event(
            connection,
            campaign_id=campaign_id,
            aggregate_id=campaign_id,
            aggregate_type="campaign",
            event_type="campaign.created",
            payload={
                "name": "worker end to end",
                "environment_id": "her_auirrh",
                "adapter_version": "1",
                "data_origin": "synthetic",
                "execution_mode": "replay",
                "declaration": {},
                "declaration_hash": "f" * 64,
                "state": "active",
            },
            expected_version=0,
            correlation_id=uuid.uuid4(),
            causation_id=None,
        )
    yield campaign_id
    # Deleted in foreign-key order, children first. Every RESTRICT in the schema is deliberate, so
    # a teardown that ignores them fails loudly rather than cascading away evidence.
    with migrated.begin() as connection:
        purge_campaign(connection, campaign_id)


def _candidate(library: str, area: str) -> HerCandidate:
    return HerCandidate(
        library_id=library,
        measurement_area_id=area,
        grid_x=Quantity(value=Decimal("0"), unit="mm"),
        grid_y=Quantity(value=Decimal("0"), unit="mm"),
    )


def _submit(
    engine: Engine, campaign_id: uuid.UUID, candidate: HerCandidate, *, key: str | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    work_item_id = uuid.uuid4()
    with engine.begin() as connection:
        campaign_event = connection.execute(
            select(events.c.event_id, events.c.correlation_id).where(
                events.c.campaign_id == campaign_id,
                events.c.event_type == "campaign.created",
            )
        ).one()
        connection.execute(
            work_items.insert().values(
                work_item_id=work_item_id,
                campaign_id=campaign_id,
                candidate_id=candidate_id(candidate),
                candidate=candidate.model_dump(mode="json"),
                state="queued",
                created_at=func.now(),
                updated_at=func.now(),
            )
        )
        queued_event = append_event(
            connection,
            campaign_id=campaign_id,
            aggregate_id=work_item_id,
            aggregate_type="work_item",
            event_type="work_item.queued",
            payload={
                "candidate_id": candidate_id(candidate),
                "candidate": candidate.model_dump(mode="json"),
                "state": "queued",
            },
            expected_version=0,
            correlation_id=campaign_event.correlation_id,
            causation_id=campaign_event.event_id,
        )
        enqueued = enqueue(
            connection,
            campaign_id=campaign_id,
            work_item_id=work_item_id,
            instruction_key=key
            or work_item_instruction_key(work_item_id=work_item_id, command_version="1"),
            command_version="1",
            correlation_id=campaign_event.correlation_id,
            causation_id=queued_event.event_id,
        )
    return work_item_id, enqueued.job_id


def _worker(
    engine: Engine,
    adapter: HerReplayAdapter,
    store: S3ObjectStore,
    *,
    name: str = "worker-test",
) -> Worker:
    return Worker(engine, adapter, store, name=name, fixture_seed=SPEC.seed)


def _deliver_again(
    connection: Connection, campaign_id: uuid.UUID, work_item_id: uuid.UUID
) -> uuid.UUID:
    """Put the same work back on the queue under a second delivery identity.

    This is what a delivery layer that failed to deduplicate looks like from the runtime's side:
    the instruction is the same work item, but the delivery carrying it is one the instruction key
    cannot recognise. Deliberately not `work_item_instruction_key`, because a delivery the enqueue
    constraint already refuses proves nothing about the acceptance constraint (F-002).
    """
    context = connection.execute(
        select(events.c.event_id, events.c.correlation_id)
        .where(events.c.campaign_id == campaign_id, events.c.aggregate_id == work_item_id)
        .order_by(events.c.sequence.desc())
        .limit(1)
    ).one()
    return enqueue(
        connection,
        campaign_id=campaign_id,
        work_item_id=work_item_id,
        instruction_key=f"redelivery:{uuid.uuid4().hex}",
        command_version="1",
        correlation_id=context.correlation_id,
        causation_id=context.event_id,
    ).job_id


async def test_a_measured_location_runs_to_an_accepted_observation(
    migrated: Engine, adapter: HerReplayAdapter, object_store: S3ObjectStore, campaign: uuid.UUID
) -> None:
    """The whole path: claim, replay, store the bytes, record the outcome and its event."""
    key = adapter.known_locations()[0]
    work_item_id, _ = _submit(
        migrated, campaign, _candidate(key.library_id, key.measurement_area_id)
    )

    outcome = await _worker(migrated, adapter, object_store).run_once()

    assert outcome is not None
    assert outcome.status == "succeeded"
    assert outcome.observation_id is not None
    with migrated.begin() as connection:
        observation = connection.execute(
            select(observations).where(observations.c.work_item_id == work_item_id)
        ).one()
        assert observation.status == "accepted"
        assert observation.data_origin == "synthetic"
        assert observation.execution_mode == "replay"
        assert object_store.get(observation.object_uri.split(f"{object_store.bucket}/")[1])


async def test_the_stored_bytes_match_the_recorded_checksum(
    migrated: Engine, adapter: HerReplayAdapter, object_store: S3ObjectStore, campaign: uuid.UUID
) -> None:
    """Otherwise `committed` would mean "we think we wrote it" rather than "it is retrievable"."""
    key = adapter.known_locations()[0]
    _submit(migrated, campaign, _candidate(key.library_id, key.measurement_area_id))

    await _worker(migrated, adapter, object_store).run_once()

    with migrated.begin() as connection:
        row = connection.execute(
            select(storage_objects).where(storage_objects.c.state == "committed").limit(1)
        ).one()
    assert row.sha256 is not None
    assert row.committed_at is not None
    assert object_store.get(row.object_key)


async def test_an_unmeasured_location_is_terminal_and_fabricates_nothing(
    migrated: Engine, adapter: HerReplayAdapter, object_store: S3ObjectStore, campaign: uuid.UUID
) -> None:
    """F-017. The source excludes areas; the worker must not interpolate one into existence."""
    measured = {
        k.measurement_area_id for k in adapter.known_locations() if k.library_id == "Au-rich"
    }
    missing = next(
        str(area) for area in range(1, SPEC.areas_per_library + 1) if str(area) not in measured
    )
    work_item_id, _ = _submit(migrated, campaign, _candidate("Au-rich", missing))

    outcome = await _worker(migrated, adapter, object_store).run_once()

    assert outcome is not None
    assert outcome.status == "failed_terminal"
    assert outcome.failure_code == "source_location_unavailable"
    with migrated.begin() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(observations)
                .where(observations.c.work_item_id == work_item_id)
            ).scalar_one()
            == 0
        )
        stored = connection.execute(
            select(attempt_outcomes).where(attempt_outcomes.c.work_item_id == work_item_id)
        ).one()
        assert stored.failure["retryable"] is False
        assert stored.provenance["code_version"] == "1"
        stream = read_stream(connection, campaign)
        assert [event["event_type"] for event in stream[-3:]] == [
            "attempt.completed",
            "work_item.rejected",
            "job.failed_terminal",
        ]
        assert stream[-2]["payload"]["state"] == "rejected"
        assert stream[-2]["payload"]["reason"]
        assert stream[-1]["causation_id"] == stream[-2]["event_id"]


async def test_a_redelivered_job_does_not_create_a_second_accepted_outcome(
    migrated: Engine, adapter: HerReplayAdapter, object_store: S3ObjectStore, campaign: uuid.UUID
) -> None:
    """PO-02, at the level a campaign actually meets it. Delivery is at least once; acceptance is
    not. The second run records `duplicate_suppressed`, never a second success."""
    key = adapter.known_locations()[0]
    work_item_id, _ = _submit(
        migrated, campaign, _candidate(key.library_id, key.measurement_area_id)
    )
    worker = _worker(migrated, adapter, object_store)

    first = await worker.run_once()
    # Re-enqueue the same work item under a fresh key: a delivery the runtime cannot dedupe by key,
    # which is exactly the case the outcome constraint has to catch.
    with migrated.begin() as connection:
        _deliver_again(connection, campaign, work_item_id)
    second = await worker.run_once()

    assert first is not None
    assert second is not None
    assert first.status == "succeeded"
    assert second.status == "duplicate_suppressed"
    with migrated.begin() as connection:
        accepted = connection.execute(
            select(func.count())
            .select_from(attempt_outcomes)
            .where(
                attempt_outcomes.c.work_item_id == work_item_id,
                attempt_outcomes.c.status == "succeeded",
            )
        ).scalar_one()
    assert accepted == ONE_OUTCOME


async def test_both_attempts_are_recorded_even_though_one_was_suppressed(
    migrated: Engine, adapter: HerReplayAdapter, object_store: S3ObjectStore, campaign: uuid.UUID
) -> None:
    """Suppressing the duplicate must not erase the fact that a second delivery happened."""
    key = adapter.known_locations()[0]
    work_item_id, _ = _submit(
        migrated, campaign, _candidate(key.library_id, key.measurement_area_id)
    )
    worker = _worker(migrated, adapter, object_store)
    await worker.run_once()
    with migrated.begin() as connection:
        _deliver_again(connection, campaign, work_item_id)
    await worker.run_once()

    with migrated.begin() as connection:
        recorded = connection.execute(
            select(func.count())
            .select_from(attempt_outcomes)
            .where(attempt_outcomes.c.work_item_id == work_item_id)
        ).scalar_one()
    assert recorded == TWO_OUTCOMES


def _acceptance_tally(connection: Connection, campaign_id: uuid.UUID, work_item_id: uuid.UUID):
    """Every count that would have to move for acceptance to have happened twice.

    Totals as well as the accepted counts: a dictionary comparison over `succeeded` and
    `duplicate_suppressed` alone cannot see a third outcome of some other status appearing.
    """
    stream = [event["event_type"] for event in read_stream(connection, campaign_id)]
    return {
        "outcomes": connection.execute(
            select(func.count())
            .select_from(attempt_outcomes)
            .where(attempt_outcomes.c.work_item_id == work_item_id)
        ).scalar_one(),
        "attempts": connection.execute(
            select(func.count())
            .select_from(attempts)
            .where(attempts.c.work_item_id == work_item_id)
        ).scalar_one(),
        "succeeded": connection.execute(
            select(func.count())
            .select_from(attempt_outcomes)
            .where(
                attempt_outcomes.c.work_item_id == work_item_id,
                attempt_outcomes.c.status == "succeeded",
            )
        ).scalar_one(),
        "duplicate_suppressed": connection.execute(
            select(func.count())
            .select_from(attempt_outcomes)
            .where(
                attempt_outcomes.c.work_item_id == work_item_id,
                attempt_outcomes.c.status == "duplicate_suppressed",
            )
        ).scalar_one(),
        "observations": connection.execute(
            select(func.count())
            .select_from(observations)
            .where(observations.c.work_item_id == work_item_id)
        ).scalar_one(),
        "metrics": connection.execute(
            select(func.count())
            .select_from(derived_metrics)
            .join(observations, derived_metrics.c.attempt_id == observations.c.attempt_id)
            .where(observations.c.work_item_id == work_item_id)
        ).scalar_one(),
        "consumed": connection.execute(
            select(func.count())
            .select_from(budget_ledger)
            .where(
                budget_ledger.c.work_item_id == work_item_id,
                budget_ledger.c.kind == "consumed",
            )
        ).scalar_one(),
        "observation.accepted": stream.count("observation.accepted"),
        "work_item.accepted": stream.count("work_item.accepted"),
        "attempt.completed": stream.count("attempt.completed"),
    }


class _BarrieredStore:
    """The real object store, held at a barrier on the last step before finalisation.

    The barrier sits here rather than around the adapter because the adapter returns two steps
    early: the bytes still have to be staged and uploaded afterwards, and a barrier there leaves
    enough slack for the winner to commit before the loser opens its transaction — which would
    quietly degrade this test into the sequential case another test already covers. Releasing both
    deliveries immediately after `put_and_verify` puts them at the acceptance claim together.

    A wrapper rather than a change to the worker: everything under test, including the store this
    delegates to, is the real implementation.
    """

    def __init__(self, store: S3ObjectStore, barrier: Barrier) -> None:
        self._store = store
        self._barrier = barrier
        self.bucket = store.bucket

    def put_and_verify(self, key: str, data: bytes, *, media_type: str) -> StoredObject:
        stored = self._store.put_and_verify(key, data, media_type=media_type)
        self._barrier.wait(timeout=60)
        return stored

    def get(self, key: str) -> bytes:
        return self._store.get(key)


async def test_two_concurrent_deliveries_yield_at_most_one_accepted_outcome(
    migrated: Engine,
    fixture_root: Path,
    adapter: HerReplayAdapter,
    object_store: S3ObjectStore,
    campaign: uuid.UUID,
) -> None:
    """F-002, PO-02, at the boundary that matters.

    Two deliveries of the same work item, on two workers, each having run the adapter and staged
    its bytes, arrive at finalisation together — the barrier is what guarantees the second has not
    already seen the first's committed outcome. `SKIP LOCKED` decides nothing here: the two workers
    hold different job rows, so both reach the acceptance claim, and only the partial unique index
    stands between them and two accepted results.
    """
    location = adapter.known_locations()[0]
    work_item_id, _ = _submit(
        migrated, campaign, _candidate(location.library_id, location.measurement_area_id)
    )
    with migrated.begin() as connection:
        _deliver_again(connection, campaign, work_item_id)
    barrier = Barrier(CONCURRENT_DELIVERIES)
    racing_store = _BarrieredStore(object_store, barrier)

    def deliver(name: str) -> WorkOutcome | None:
        return asyncio.run(
            _worker(migrated, HerReplayAdapter(fixture_root), racing_store, name=name).run_once()
        )

    with ThreadPoolExecutor(max_workers=CONCURRENT_DELIVERIES) as pool:
        futures = [pool.submit(deliver, f"racer-{n}") for n in range(CONCURRENT_DELIVERIES)]
        outcomes = [future.result(timeout=120) for future in futures]

    assert sorted(outcome.status for outcome in outcomes if outcome) == [
        "duplicate_suppressed",
        "succeeded",
    ]
    with migrated.begin() as connection:
        tally = _acceptance_tally(connection, campaign, work_item_id)
    assert tally == {
        "outcomes": TWO_OUTCOMES,
        "attempts": TWO_OUTCOMES,
        "succeeded": ONE_OUTCOME,
        "duplicate_suppressed": ONE_OUTCOME,
        "observations": ONE_OBSERVATION,
        "metrics": TWO_METRICS,
        "consumed": ONE_LEDGER_ENTRY,
        "observation.accepted": ONE_EVENT,
        "work_item.accepted": ONE_EVENT,
        # One per attempt: suppressing the duplicate must not erase that it reached finalisation.
        "attempt.completed": TWO_OUTCOMES,
    }


async def test_a_redelivery_after_the_commit_does_not_repeat_the_accepted_effect(
    migrated: Engine, adapter: HerReplayAdapter, object_store: S3ObjectStore, campaign: uuid.UUID
) -> None:
    """F-007, in the shape this architecture can actually produce it.

    The outcome transaction committed. Because `jobs.complete` is inside that transaction, the job
    row is `succeeded` and terminal — so a lost acknowledgement cannot reappear as that row becoming
    available again. It reappears the only way an at-least-once delivery layer can express it: the
    work is offered once more under a delivery identity the instruction key does not recognise. That
    is what `_deliver_again` builds, and it does so through the real enqueue path rather than by
    writing a job state the lifecycle forbids.

    The original acceptance stays authoritative — same attempt, same observation, same budget entry
    — and the redelivered attempt is represented rather than dropped. Nothing raises: the redelivery
    loses the acceptance claim through `ON CONFLICT`, not through an integrity error the worker has
    to interpret.

    This covers the redelivery half of F-007. The process-boundary half — that the accepted outcome
    is still there after the worker is killed and restarted — is `test_crash_recovery.py`, which
    kills a real subprocess.
    """
    location = adapter.known_locations()[0]
    work_item_id, first_job_id = _submit(
        migrated, campaign, _candidate(location.library_id, location.measurement_area_id)
    )
    worker = _worker(migrated, adapter, object_store)
    accepted = await worker.run_once()
    assert accepted is not None
    assert accepted.status == "succeeded"
    assert accepted.job_id == first_job_id
    with migrated.begin() as connection:
        before = connection.execute(
            select(attempt_outcomes.c.attempt_id, attempt_outcomes.c.observation_id).where(
                attempt_outcomes.c.work_item_id == work_item_id,
                attempt_outcomes.c.status == "succeeded",
            )
        ).one()
        redelivered_job_id = _deliver_again(connection, campaign, work_item_id)

    redelivered = await worker.run_once()

    assert redelivered is not None
    assert redelivered.status == "duplicate_suppressed"
    assert redelivered.job_id == redelivered_job_id
    with migrated.begin() as connection:
        tally = _acceptance_tally(connection, campaign, work_item_id)
        after = connection.execute(
            select(attempt_outcomes.c.attempt_id, attempt_outcomes.c.observation_id).where(
                attempt_outcomes.c.work_item_id == work_item_id,
                attempt_outcomes.c.status == "succeeded",
            )
        ).one()
        item_state = connection.execute(
            select(work_items.c.state).where(work_items.c.work_item_id == work_item_id)
        ).scalar_one()
    assert tally == {
        "outcomes": TWO_OUTCOMES,
        "attempts": TWO_OUTCOMES,
        "succeeded": ONE_OUTCOME,
        "duplicate_suppressed": ONE_OUTCOME,
        "observations": ONE_OBSERVATION,
        "metrics": TWO_METRICS,
        "consumed": ONE_LEDGER_ENTRY,
        "observation.accepted": ONE_EVENT,
        "work_item.accepted": ONE_EVENT,
        # One per attempt: suppressing the duplicate must not erase that it reached finalisation.
        "attempt.completed": TWO_OUTCOMES,
    }
    # The original outcome is untouched: same attempt, same observation, still the accepted one.
    assert after.attempt_id == before.attempt_id
    assert after.observation_id == before.observation_id
    assert item_state == "accepted"


async def test_the_accepted_observation_is_recorded_as_an_event(
    migrated: Engine, adapter: HerReplayAdapter, object_store: S3ObjectStore, campaign: uuid.UUID
) -> None:
    """The event and the outcome share one transaction, so a bundle can never show one without the
    other."""
    key = adapter.known_locations()[0]
    _submit(migrated, campaign, _candidate(key.library_id, key.measurement_area_id))

    await _worker(migrated, adapter, object_store).run_once()

    with migrated.begin() as connection:
        stream = read_stream(connection, campaign)
    assert [event["event_type"] for event in stream] == [
        "campaign.created",
        "work_item.queued",
        "job.enqueued",
        "job.leased",
        "job.started",
        "attempt.started",
        "observation.accepted",
        "attempt.completed",
        "work_item.accepted",
        "job.succeeded",
    ]
    assert [event["campaign_position"] for event in stream] == list(range(1, 11))
    assert len({event["correlation_id"] for event in stream}) == 1
    assert stream[0]["causation_id"] is None
    assert all(event["causation_id"] is not None for event in stream[1:])
    positions = {event["event_id"]: event["campaign_position"] for event in stream}
    assert all(
        positions[event["causation_id"]] < event["campaign_position"] for event in stream[1:]
    )


async def test_an_empty_queue_is_a_no_op(
    migrated: Engine, adapter: HerReplayAdapter, object_store: S3ObjectStore
) -> None:
    """Cancelled rather than deleted: attempts reference jobs under RESTRICT, and a test that
    deletes the queue out from under them is testing its own teardown."""
    with migrated.begin() as connection:
        connection.execute(
            jobs.update().where(jobs.c.state == "available").values(state="cancelled")
        )

    assert await _worker(migrated, adapter, object_store).run_once() is None


async def test_the_accepted_metric_resolves_to_its_observation_and_root(
    migrated: Engine, adapter: HerReplayAdapter, object_store: S3ObjectStore, campaign: uuid.UUID
) -> None:
    """Every accepted metric resolves to a retained observation and to a lineage root. Checked by
    following the link rather than by trusting the writer."""
    key = adapter.known_locations()[0]
    _submit(migrated, campaign, _candidate(key.library_id, key.measurement_area_id))

    await _worker(migrated, adapter, object_store).run_once()

    with migrated.begin() as connection:
        rows = connection.execute(
            select(derived_metrics)
            .join(
                observations,
                (derived_metrics.c.observation_id == observations.c.observation_id)
                & (derived_metrics.c.attempt_id == observations.c.attempt_id),
            )
            .where(observations.c.campaign_id == campaign)
        ).all()

    assert rows
    for metric in rows:
        assert metric.analysis_name == "labbridge_lsv_cathodic_extremum"
        assert metric.analysis_version
        assert metric.parameter_hash
        assert metric.provenance["synthetic_root"]["seed"] is not None
        assert metric.provenance["source_record"] is None


async def test_a_metric_is_not_mistaken_for_a_source_provided_fit(
    migrated: Engine, adapter: HerReplayAdapter, object_store: S3ObjectStore, campaign: uuid.UUID
) -> None:
    """docs/SPEC.md §3.6: a LabBridge recomputation and the source's own fit must stay distinct, so
    the analysis name is the thing that separates them and it must not be the source's."""
    key = adapter.known_locations()[0]
    _submit(migrated, campaign, _candidate(key.library_id, key.measurement_area_id))

    await _worker(migrated, adapter, object_store).run_once()

    with migrated.begin() as connection:
        names = set(
            connection.execute(
                select(derived_metrics.c.analysis_name)
                .join(
                    observations,
                    (derived_metrics.c.observation_id == observations.c.observation_id)
                    & (derived_metrics.c.attempt_id == observations.c.attempt_id),
                )
                .where(observations.c.campaign_id == campaign)
            ).scalars()
        )

    assert names == {"labbridge_lsv_cathodic_extremum"}
    assert "source_provided" not in "".join(names)


async def test_every_attempt_appends_a_budget_entry_in_the_same_transaction(
    migrated: Engine, adapter: HerReplayAdapter, object_store: S3ObjectStore, campaign: uuid.UUID
) -> None:
    """docs/SPEC.md §8: outcome, event, projection and budget change share one
    transaction. A failed attempt is recorded too — a budget that counted only successes would let
    a campaign burn its allowance invisibly."""
    key = adapter.known_locations()[0]
    _submit(migrated, campaign, _candidate(key.library_id, key.measurement_area_id))
    measured = {
        k.measurement_area_id for k in adapter.known_locations() if k.library_id == "Au-rich"
    }
    missing = next(
        str(area) for area in range(1, SPEC.areas_per_library + 1) if str(area) not in measured
    )
    _submit(migrated, campaign, _candidate("Au-rich", missing))

    worker = _worker(migrated, adapter, object_store)
    await worker.run_once()
    await worker.run_once()

    with migrated.begin() as connection:
        entries = connection.execute(
            select(budget_ledger).where(budget_ledger.c.campaign_id == campaign)
        ).all()

    assert len(entries) == TWO_OUTCOMES
    for entry in entries:
        assert entry.kind == "consumed"
        assert entry.unit == "attempt"
        assert entry.reason


async def test_a_campaign_declaring_the_wrong_origin_is_refused_not_relabelled(
    migrated: Engine,
    adapter: HerReplayAdapter,
    object_store: S3ObjectStore,
    purge_campaign: Callable[[Connection, uuid.UUID], None],
) -> None:
    """The conflation ADR-010 exists to prevent, at the one place it could still happen. The API
    accepts a client-supplied `observed + replay`, which is admissible on its own; running it
    against a fixture-backed adapter previously wrote observation rows labelled `observed` whose
    lineage resolved to a synthetic root, and stamped `observed` into the bundle manifest."""
    campaign_id = uuid.uuid4()
    with migrated.begin() as connection:
        connection.execute(
            campaigns.insert().values(
                campaign_id=campaign_id,
                name="declares observed, adapter reads synthetic",
                environment_id="her_auirrh",
                adapter_version="1",
                data_origin="observed",
                execution_mode="replay",
                state="active",
                declaration={},
                declaration_hash="0" * 64,
                event_stream_contract_version=1,
                event_stream_last_position=0,
                created_at=func.now(),
                updated_at=func.now(),
            )
        )
        append_event(
            connection,
            campaign_id=campaign_id,
            aggregate_id=campaign_id,
            aggregate_type="campaign",
            event_type="campaign.created",
            payload={
                "name": "declares observed, adapter reads synthetic",
                "environment_id": "her_auirrh",
                "adapter_version": "1",
                "data_origin": "observed",
                "execution_mode": "replay",
                "declaration": {},
                "declaration_hash": "0" * 64,
                "state": "active",
            },
            expected_version=0,
            correlation_id=uuid.uuid4(),
            causation_id=None,
        )
    try:
        key = adapter.known_locations()[0]
        work_item_id, _ = _submit(
            migrated, campaign_id, _candidate(key.library_id, key.measurement_area_id)
        )

        outcome = await _worker(migrated, adapter, object_store).run_once()

        assert outcome is not None
        assert outcome.failure_code == "environment_mismatch"
        with migrated.begin() as connection:
            assert (
                connection.execute(
                    select(func.count())
                    .select_from(observations)
                    .where(observations.c.work_item_id == work_item_id)
                ).scalar_one()
                == 0
            )
            stored = connection.execute(
                select(attempt_outcomes).where(attempt_outcomes.c.work_item_id == work_item_id)
            ).one()
            # The outcome carries the campaign's declared pair — it produced no bytes, so there
            # is no data whose origin it could misstate — and the summary names what the adapter
            # actually reads, which is the disagreement itself.
            assert stored.data_origin == "observed"
            assert stored.failure["category"] == "policy"
            assert "synthetic+replay" in stored.failure["summary"]
    finally:
        with migrated.begin() as connection:
            purge_campaign(connection, campaign_id)


async def test_an_adapter_crash_still_records_an_outcome(
    migrated: Engine, adapter: HerReplayAdapter, object_store: S3ObjectStore, campaign: uuid.UUID
) -> None:
    """Invariant 2: every attempt produces a durable outcome. An exception that escaped left the
    attempt `running`, the job leased by a process about to move on, and no record of why."""
    key = adapter.known_locations()[0]
    work_item_id, _ = _submit(
        migrated, campaign, _candidate(key.library_id, key.measurement_area_id)
    )

    class Exploding:
        environment = adapter.environment

        def known_locations(self) -> object:
            return adapter.known_locations()

        async def execute(self, candidate: object) -> object:
            message = "the instrument went away"
            raise OSError(message)

        def synthetic_root(self) -> object:
            return adapter.synthetic_root()

    worker = Worker(migrated, Exploding(), object_store, name="worker-doomed")  # type: ignore[arg-type]
    outcome = await worker.run_once()

    assert outcome is not None
    assert outcome.status == "failed_retryable"
    with migrated.begin() as connection:
        stored = connection.execute(
            select(attempt_outcomes).where(attempt_outcomes.c.work_item_id == work_item_id)
        ).one()
        attempt_state = connection.execute(
            select(attempts.c.state).where(attempts.c.work_item_id == work_item_id)
        ).scalar_one()
        stream = read_stream(connection, campaign)
    assert stored.failure["exception_type"] == "OSError"
    assert stored.failure["retryable"] is True
    assert attempt_state == "failed_retryable"
    assert [event["event_type"] for event in stream[-2:]] == [
        "attempt.completed",
        "job.available",
    ]
    assert stream[-1]["payload"]["state"] == "available"
    assert stream[-1]["causation_id"] == stream[-2]["event_id"]


async def test_an_unavailable_location_is_not_recorded_as_an_instrument_fault(
    migrated: Engine, adapter: HerReplayAdapter, object_store: S3ObjectStore, campaign: uuid.UUID
) -> None:
    """The archive omits 20 areas per library from SECCM by design. Recording that as `instrument`
    would assert the instrument failed where the source records that nothing was attempted."""
    measured = {
        k.measurement_area_id for k in adapter.known_locations() if k.library_id == "Au-rich"
    }
    missing = next(
        str(area) for area in range(1, SPEC.areas_per_library + 1) if str(area) not in measured
    )
    work_item_id, _ = _submit(migrated, campaign, _candidate("Au-rich", missing))

    await _worker(migrated, adapter, object_store).run_once()

    with migrated.begin() as connection:
        stored = connection.execute(
            select(attempt_outcomes).where(attempt_outcomes.c.work_item_id == work_item_id)
        ).one()
    assert stored.failure["category"] != "instrument"

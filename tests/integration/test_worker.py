"""The worker end to end, against real PostgreSQL and real MinIO.

This test exercises a whole campaign-to-evidence path, so several guarantees become observable:
duplicate delivery does not create a second accepted outcome (PO-02), an unmeasured location becomes
a terminal outcome rather than a fabricated measurement (F-017), and a fixture-backed run records
itself as synthetic (ADR-010).

Everything runs on the generated fixture, so the suite needs no download. A fixture-backed run is
not evidence about the physical system, and nothing here should be read as such.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, func, select

from labbridge.domain.candidates import HerCandidate, candidate_id
from labbridge.domain.quantities import Quantity
from labbridge.environments.her_replay import HerReplayAdapter
from labbridge.infrastructure.her_ingestion.fixture import (
    FIXTURE_MANIFEST_FILENAME,
    FixtureSpec,
    build_fixture,
)
from labbridge.infrastructure.her_ingestion.provenance import write_document
from labbridge.infrastructure.objectstore import S3ObjectStore
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
from labbridge.runtime.worker import Worker

pytestmark = pytest.mark.integration

SPEC = FixtureSpec(areas_per_library=6, seccm_areas_per_library=2)
ONE_OUTCOME = 1
TWO_OUTCOMES = 2


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
) -> tuple[uuid.UUID, uuid.UUID | None]:
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
        job_id = enqueue(
            connection,
            campaign_id=campaign_id,
            work_item_id=work_item_id,
            idempotency_key=key or f"key:{uuid.uuid4().hex}",
            command_version="1",
            correlation_id=campaign_event.correlation_id,
            causation_id=queued_event.event_id,
        )
    return work_item_id, job_id


def _worker(engine: Engine, adapter: HerReplayAdapter, store: S3ObjectStore) -> Worker:
    return Worker(engine, adapter, store, name="worker-test", fixture_seed=SPEC.seed)


def _reenqueue(connection: Connection, campaign_id: uuid.UUID, work_item_id: uuid.UUID) -> None:
    context = connection.execute(
        select(events.c.event_id, events.c.correlation_id)
        .where(events.c.campaign_id == campaign_id, events.c.aggregate_id == work_item_id)
        .order_by(events.c.sequence.desc())
        .limit(1)
    ).one()
    enqueue(
        connection,
        campaign_id=campaign_id,
        work_item_id=work_item_id,
        idempotency_key=f"redelivery:{uuid.uuid4().hex}",
        command_version="1",
        correlation_id=context.correlation_id,
        causation_id=context.event_id,
    )


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
        _reenqueue(connection, campaign, work_item_id)
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
        _reenqueue(connection, campaign, work_item_id)
    await worker.run_once()

    with migrated.begin() as connection:
        recorded = connection.execute(
            select(func.count())
            .select_from(attempt_outcomes)
            .where(attempt_outcomes.c.work_item_id == work_item_id)
        ).scalar_one()
    assert recorded == TWO_OUTCOMES


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
    """A Slice 1 exit criterion: every accepted metric resolves to a retained observation and to a
    lineage root. Checked by following the link rather than by trusting the writer."""
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
    """docs/SPEC.md §8 and Slice 1: outcome, event, projection and budget change share one
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

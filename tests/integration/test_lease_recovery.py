"""Lease ownership, heartbeats, fencing, and reconciliation, against real PostgreSQL and MinIO.

Every property here is a property of what the *database* believes about ownership, so nothing is
substituted: no fake clock, no in-memory queue, no stubbed store. Leases are compressed to a couple
of seconds rather than mocked, because a test that moved the clock forward would prove something
about the mock and not about `now()` evaluated server-side.

The distinction the whole file turns on: a worker never knows it still owns a job. It knows what the
database said last time it asked, and the fencing token is what makes a stale answer detectable.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, func, select

from labbridge import cli
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
from labbridge.infrastructure.objectstore import S3ObjectStore
from labbridge.infrastructure.persistence.tables import (
    attempt_outcomes,
    attempts,
    campaigns,
    events,
    jobs,
    observations,
    storage_objects,
    work_items,
)
from labbridge.runtime.events import append_event
from labbridge.runtime.heartbeat import Heartbeat
from labbridge.runtime.jobs import (
    LeaseLostError,
    assert_held,
    claim,
    complete,
    enqueue,
    expire_lease_now,
    heartbeat,
    recover_expired_leases,
)
from labbridge.runtime.reconciliation import reconcile
from labbridge.runtime.worker import Worker, WorkOutcome

pytestmark = pytest.mark.integration

SPEC = FixtureSpec(areas_per_library=6, seccm_areas_per_library=2)
ONE = 1
TWO = 2
#: Short enough that a lease genuinely lapses inside a test, long enough not to lapse by accident
#: between two statements on a loaded machine.
SHORT_LEASE_SECONDS = 2
FAST_HEARTBEAT_SECONDS = 0.2


@pytest.fixture(scope="session")
def lease_fixture_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("lease-fixture")
    manifest = build_fixture(root, spec=SPEC, generator_version="0.1.0")
    write_document(root / FIXTURE_MANIFEST_FILENAME, manifest)
    return root


@pytest.fixture
def adapter(lease_fixture_root: Path) -> HerReplayAdapter:
    return HerReplayAdapter(lease_fixture_root)


@pytest.fixture
def campaign(
    migrated: Engine, purge_campaign: Callable[[Connection, uuid.UUID], None]
) -> Iterator[uuid.UUID]:
    campaign_id = uuid.uuid4()
    declaration_hash = "b" * 64
    with migrated.begin() as connection:
        connection.execute(
            campaigns.insert().values(
                campaign_id=campaign_id,
                name="lease recovery",
                environment_id="her_auirrh",
                adapter_version="1",
                data_origin="synthetic",
                execution_mode="replay",
                state="active",
                declaration={},
                declaration_hash=declaration_hash,
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
                "name": "lease recovery",
                "environment_id": "her_auirrh",
                "adapter_version": "1",
                "data_origin": "synthetic",
                "execution_mode": "replay",
                "declaration": {},
                "declaration_hash": declaration_hash,
                "state": "active",
            },
            expected_version=0,
            correlation_id=uuid.uuid4(),
            causation_id=None,
        )
    yield campaign_id
    with migrated.begin() as connection:
        purge_campaign(connection, campaign_id)


def _submit(engine: Engine, campaign_id: uuid.UUID, adapter: HerReplayAdapter) -> uuid.UUID:
    location = adapter.known_locations()[0]
    candidate = HerCandidate(
        library_id=location.library_id,
        measurement_area_id=location.measurement_area_id,
        grid_x=Quantity(value=Decimal("0"), unit="mm"),
        grid_y=Quantity(value=Decimal("0"), unit="mm"),
    )
    work_item_id = uuid.uuid4()
    with engine.begin() as connection:
        root = connection.execute(
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
        queued = append_event(
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
            correlation_id=root.correlation_id,
            causation_id=root.event_id,
        )
        enqueue(
            connection,
            campaign_id=campaign_id,
            work_item_id=work_item_id,
            instruction_key=work_item_instruction_key(
                work_item_id=work_item_id, command_version="1"
            ),
            command_version="1",
            correlation_id=root.correlation_id,
            causation_id=queued.event_id,
        )
    return work_item_id


def _generation(engine: Engine, work_item_id: uuid.UUID) -> int:
    with engine.begin() as connection:
        return int(
            connection.execute(
                select(jobs.c.lease_generation).where(jobs.c.work_item_id == work_item_id)
            ).scalar_one()
        )


def test_a_heartbeating_worker_keeps_its_lease_past_one_nominal_duration(
    migrated: Engine, campaign: uuid.UUID, adapter: HerReplayAdapter
) -> None:
    """The point of a heartbeat: work longer than a lease without stranding the job for that long
    if the process dies. Waited out in real time rather than by moving a clock — the expiry is
    evaluated by the database, and a mocked clock would not reach it."""
    _submit(migrated, campaign, adapter)
    with migrated.begin() as connection:
        lease = claim(connection, owner="worker-alive", lease_seconds=SHORT_LEASE_SECONDS)
    assert lease is not None

    with Heartbeat(
        migrated,
        lease,
        interval_seconds=FAST_HEARTBEAT_SECONDS,
        lease_seconds=SHORT_LEASE_SECONDS,
    ) as beating:
        time.sleep(SHORT_LEASE_SECONDS * 1.5)
        # Still held, past the point where an unextended lease would have lapsed.
        with migrated.begin() as connection:
            assert_held(connection, lease)
        assert beating.lost is None
        assert beating.beats >= ONE

    # And nothing reclaims it while it is alive.
    with migrated.begin() as connection:
        assert recover_expired_leases(connection) == []


def test_a_worker_that_stops_heartbeating_is_reclaimed(
    migrated: Engine, campaign: uuid.UUID, adapter: HerReplayAdapter
) -> None:
    """The other half: liveness has to be proven, not assumed."""
    work_item_id = _submit(migrated, campaign, adapter)
    with migrated.begin() as connection:
        lease = claim(connection, owner="worker-silent", lease_seconds=SHORT_LEASE_SECONDS)
    assert lease is not None
    # No heartbeat at all: the lease simply runs out.
    time.sleep(SHORT_LEASE_SECONDS + 0.5)

    with migrated.begin() as connection:
        reclaimed = recover_expired_leases(connection)

    assert len(reclaimed) == ONE
    assert reclaimed[0].previous_owner == "worker-silent"
    assert reclaimed[0].fenced_generation == lease.lease_generation
    with migrated.begin() as connection:
        state = connection.execute(
            select(jobs.c.state, jobs.c.lease_owner, jobs.c.lease_token).where(
                jobs.c.work_item_id == work_item_id
            )
        ).one()
    assert state.state == "available"
    assert state.lease_owner is None
    assert state.lease_token is None


def test_reclaiming_a_lease_advances_the_fencing_token(
    migrated: Engine, campaign: uuid.UUID, adapter: HerReplayAdapter
) -> None:
    """Monotonic, and advanced at the reclaim rather than at the next claim. Waiting for a claim
    would leave a window in which the fenced-out holder still looked current."""
    work_item_id = _submit(migrated, campaign, adapter)
    with migrated.begin() as connection:
        first = claim(connection, owner="worker-a", lease_seconds=SHORT_LEASE_SECONDS)
    assert first is not None
    assert first.lease_generation == ONE

    with migrated.begin() as connection:
        expire_lease_now(connection, first.job_id)
        reclaimed = recover_expired_leases(connection)

    assert len(reclaimed) == ONE
    assert reclaimed[0].lease_generation > reclaimed[0].fenced_generation
    assert _generation(migrated, work_item_id) == TWO

    with migrated.begin() as connection:
        second = claim(connection, owner="worker-b", lease_seconds=SHORT_LEASE_SECONDS)
    assert second is not None
    # Strictly greater than both the original lease and the reclaim.
    assert second.lease_generation > reclaimed[0].lease_generation


def test_a_stale_fencing_token_cannot_finalise(
    migrated: Engine, campaign: uuid.UUID, adapter: HerReplayAdapter
) -> None:
    """The core of the fence. The stale worker's own lease object is unchanged — its token and
    expiry still look valid *to it* — and every ownership-bearing operation still refuses."""
    _submit(migrated, campaign, adapter)
    with migrated.begin() as connection:
        stale = claim(connection, owner="worker-stale", lease_seconds=SHORT_LEASE_SECONDS)
    assert stale is not None
    with migrated.begin() as connection:
        expire_lease_now(connection, stale.job_id)
        recover_expired_leases(connection)
        fresh = claim(connection, owner="worker-fresh", lease_seconds=SHORT_LEASE_SECONDS)
    assert fresh is not None
    assert fresh.lease_generation > stale.lease_generation

    with migrated.begin() as connection:
        with pytest.raises(LeaseLostError):
            assert_held(connection, stale)
        with pytest.raises(LeaseLostError):
            heartbeat(connection, stale)
        with pytest.raises(LeaseLostError):
            complete(connection, stale)
        # The new holder is unaffected by any of that.
        assert_held(connection, fresh)


def test_a_token_that_matches_but_a_generation_that_does_not_is_refused(
    migrated: Engine, campaign: uuid.UUID, adapter: HerReplayAdapter
) -> None:
    """Why the generation exists alongside the token. A reclaim that handed the job back to the
    same owner would leave owner and expiry looking right; only the generation has moved."""
    _submit(migrated, campaign, adapter)
    with migrated.begin() as connection:
        held = claim(connection, owner="worker-a", lease_seconds=SHORT_LEASE_SECONDS)
    assert held is not None

    stale = replace(held, lease_generation=held.lease_generation - 1)

    with migrated.begin() as connection:
        # Same job, same token, same live expiry — refused purely on the generation.
        with pytest.raises(LeaseLostError):
            assert_held(connection, stale)
        assert_held(connection, held)


def test_an_expired_lease_leads_to_a_safe_new_attempt(
    migrated: Engine,
    campaign: uuid.UUID,
    adapter: HerReplayAdapter,
    object_store: S3ObjectStore,
) -> None:
    """Reclaim, re-execute, accept exactly once. The first attempt is preserved as its own record
    rather than rewritten (docs/SPEC.md §7.3)."""
    work_item_id = _submit(migrated, campaign, adapter)
    with migrated.begin() as connection:
        abandoned = claim(connection, owner="worker-gone", lease_seconds=SHORT_LEASE_SECONDS)
        assert abandoned is not None
        connection.execute(
            attempts.insert().values(
                attempt_id=uuid.uuid4(),
                work_item_id=work_item_id,
                job_id=abandoned.job_id,
                ordinal=1,
                state="running",
                started_at=func.now(),
                created_at=func.now(),
            )
        )
        expire_lease_now(connection, abandoned.job_id)

    with migrated.begin() as connection:
        report = reconcile(connection, object_store)
    assert len(report.reclaimed) == ONE
    assert len(report.closed_attempts) == ONE

    outcome = _run(migrated, adapter, object_store, name="worker-survivor")

    assert outcome is not None
    assert outcome.status == "succeeded"
    with migrated.begin() as connection:
        accepted = connection.execute(
            select(func.count())
            .select_from(observations)
            .where(
                observations.c.work_item_id == work_item_id,
                observations.c.status == "accepted",
            )
        ).scalar_one()
        states = (
            connection.execute(
                select(attempts.c.state).where(attempts.c.work_item_id == work_item_id)
            )
            .scalars()
            .all()
        )
    assert accepted == ONE
    assert sorted(states) == ["lease_lost", "succeeded"]


def test_no_known_failure_leaves_an_attempt_running(
    migrated: Engine,
    campaign: uuid.UUID,
    adapter: HerReplayAdapter,
    object_store: S3ObjectStore,
) -> None:
    """`running` means work in flight. An attempt left there by a dead process is indistinguishable
    from one still going, which is exactly the ambiguity reconciliation exists to remove."""
    work_item_id = _submit(migrated, campaign, adapter)
    with migrated.begin() as connection:
        lease = claim(connection, owner="worker-dead", lease_seconds=SHORT_LEASE_SECONDS)
        assert lease is not None
        connection.execute(
            attempts.insert().values(
                attempt_id=uuid.uuid4(),
                work_item_id=work_item_id,
                job_id=lease.job_id,
                ordinal=1,
                state="running",
                started_at=func.now(),
                created_at=func.now(),
            )
        )
        expire_lease_now(connection, lease.job_id)

    with migrated.begin() as connection:
        reconcile(connection, object_store)

    with migrated.begin() as connection:
        still_running = connection.execute(
            select(func.count())
            .select_from(attempts)
            .where(attempts.c.work_item_id == work_item_id, attempts.c.state == "running")
        ).scalar_one()
        closed = (
            connection.execute(
                select(attempts.c.state).where(attempts.c.work_item_id == work_item_id)
            )
            .scalars()
            .all()
        )
        outcomes = connection.execute(
            select(attempt_outcomes.c.status, attempt_outcomes.c.failure).where(
                attempt_outcomes.c.work_item_id == work_item_id
            )
        ).all()
        completed_events = (
            connection.execute(
                select(events.c.event_type).where(
                    events.c.aggregate_id.in_(
                        select(attempts.c.attempt_id).where(attempts.c.work_item_id == work_item_id)
                    ),
                    events.c.aggregate_type == "attempt",
                    events.c.event_type == "attempt.completed",
                )
            )
            .scalars()
            .all()
        )
    assert still_running == 0
    # `lease_lost`, not `cancelled`: nobody asked for this work to stop.
    assert closed == ["lease_lost"]
    assert [row.status for row in outcomes] == ["lease_lost"]
    assert outcomes[0].failure["failure_code"] == "lease_lost"
    assert completed_events == ["attempt.completed"]


def test_worker_startup_runs_one_reconciliation_pass(
    migrated: Engine,
    campaign: uuid.UUID,
    adapter: HerReplayAdapter,
    object_store: S3ObjectStore,
) -> None:
    """Recovery belongs where a worker is about to look for work anyway, so there is nothing extra
    to schedule and nothing that can be forgotten."""
    work_item_id = _submit(migrated, campaign, adapter)
    with migrated.begin() as connection:
        lease = claim(connection, owner="worker-previous", lease_seconds=SHORT_LEASE_SECONDS)
        assert lease is not None
        expire_lease_now(connection, lease.job_id)

    worker = Worker(migrated, adapter, object_store, name="worker-starting")
    report = worker.start()

    assert len(report.reclaimed) == ONE
    assert report.reclaimed[0].previous_owner == "worker-previous"
    with migrated.begin() as connection:
        state = connection.execute(
            select(jobs.c.state).where(jobs.c.work_item_id == work_item_id)
        ).scalar_one()
    assert state == "available"


def test_the_startup_pass_runs_before_the_first_claim_even_if_start_is_not_called(
    migrated: Engine,
    campaign: uuid.UUID,
    adapter: HerReplayAdapter,
    object_store: S3ObjectStore,
) -> None:
    """A caller that forgets `start()` must still get recovery, or the guarantee depends on
    remembering to ask for it."""
    work_item_id = _submit(migrated, campaign, adapter)
    with migrated.begin() as connection:
        lease = claim(connection, owner="worker-previous", lease_seconds=SHORT_LEASE_SECONDS)
        assert lease is not None
        expire_lease_now(connection, lease.job_id)

    # Straight to `run_once`: the reclaim is what makes the job claimable at all here.
    outcome = _run(migrated, adapter, object_store, name="worker-implicit")

    assert outcome is not None
    assert outcome.status == "succeeded"
    with migrated.begin() as connection:
        accepted = connection.execute(
            select(func.count())
            .select_from(observations)
            .where(
                observations.c.work_item_id == work_item_id,
                observations.c.status == "accepted",
            )
        ).scalar_one()
    assert accepted == ONE


def test_the_cli_command_invokes_the_same_reconciliation_service(
    migrated: Engine,
    campaign: uuid.UUID,
    adapter: HerReplayAdapter,
    object_store: S3ObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`labbridge reconcile` and worker startup must not drift into two behaviours. Asserted by
    running the command's own callable against the real database and checking it did the work."""
    work_item_id = _submit(migrated, campaign, adapter)
    with migrated.begin() as connection:
        lease = claim(connection, owner="worker-previous", lease_seconds=SHORT_LEASE_SECONDS)
        assert lease is not None
        expire_lease_now(connection, lease.job_id)

    monkeypatch.setattr(cli, "create_engine", lambda *args, **kwargs: migrated)
    monkeypatch.setattr(cli, "_build_object_store", lambda: object_store)

    cli.reconcile_command()

    with migrated.begin() as connection:
        state = connection.execute(
            select(jobs.c.state).where(jobs.c.work_item_id == work_item_id)
        ).scalar_one()
    assert state == "available"


def test_reconciliation_classifies_a_post_upload_pre_commit_object_without_accepting_it(
    migrated: Engine,
    campaign: uuid.UUID,
    adapter: HerReplayAdapter,
    object_store: S3ObjectStore,
) -> None:
    """The window the step ordering creates on purpose: bytes in the store, a `pending` row, and no
    transaction that ever committed. They are kept and named, never promoted."""
    work_item_id = _submit(migrated, campaign, adapter)
    with migrated.begin() as connection:
        lease = claim(connection, owner="worker-gone", lease_seconds=SHORT_LEASE_SECONDS)
        assert lease is not None
        attempt_id = uuid.uuid4()
        connection.execute(
            attempts.insert().values(
                attempt_id=attempt_id,
                work_item_id=work_item_id,
                job_id=lease.job_id,
                ordinal=1,
                state="running",
                started_at=func.now(),
                created_at=func.now(),
            )
        )
        key = f"observations/{campaign}/{uuid.uuid4().hex}"
        stored = object_store.put_and_verify(key, b"orphaned bytes", media_type="text/csv")
        connection.execute(
            storage_objects.insert().values(
                object_uri=stored.uri,
                bucket=object_store.bucket,
                object_key=key,
                media_type="text/csv",
                attempt_id=attempt_id,
                work_item_id=work_item_id,
                state="pending",
                created_at=func.now(),
            )
        )
        expire_lease_now(connection, lease.job_id)

    with migrated.begin() as connection:
        report = reconcile(connection, object_store)
        row = connection.execute(
            select(storage_objects).where(storage_objects.c.object_uri == stored.uri)
        ).one()
        accepted = connection.execute(
            select(func.count())
            .select_from(observations)
            .where(observations.c.work_item_id == work_item_id)
        ).scalar_one()

    verdicts = {entry.object_uri: entry.classification for entry in report.classified}
    assert verdicts[stored.uri] == "diagnostic_orphan"
    assert row.classification == "diagnostic_orphan"
    assert row.classification_reason
    assert row.reconciled_at is not None
    # The digest is recorded now that the bytes have been read, and the object is still there.
    assert row.sha256 == stored.sha256
    assert object_store.exists(key)
    # Nothing was promoted into scientific state.
    assert accepted == 0
    assert row.state == "pending"


def test_reconciliation_quarantines_a_checksum_mismatch_rather_than_repairing_it(
    migrated: Engine,
    campaign: uuid.UUID,
    adapter: HerReplayAdapter,
    object_store: S3ObjectStore,
) -> None:
    """Refreshing the recorded digest would launder corruption into evidence; deleting the object
    would destroy the proof. Both are refused, and the row keeps the digest it recorded (F-028)."""
    work_item_id = _submit(migrated, campaign, adapter)
    key = f"observations/{campaign}/{uuid.uuid4().hex}"
    stored = object_store.put_and_verify(key, b"real bytes", media_type="text/csv")
    claimed_digest = "d" * 64
    with migrated.begin() as connection:
        connection.execute(
            storage_objects.insert().values(
                object_uri=stored.uri,
                bucket=object_store.bucket,
                object_key=key,
                media_type="text/csv",
                work_item_id=work_item_id,
                sha256=claimed_digest,
                byte_size=stored.byte_size,
                state="pending",
                created_at=func.now(),
            )
        )

    with migrated.begin() as connection:
        reconcile(connection, object_store)
        row = connection.execute(
            select(storage_objects).where(storage_objects.c.object_uri == stored.uri)
        ).one()

    assert row.classification == "quarantined"
    assert claimed_digest in row.classification_reason
    assert stored.sha256 in row.classification_reason
    # Neither side was trusted: the recorded digest is untouched and the bytes are still there.
    assert row.sha256 == claimed_digest
    assert object_store.exists(key)


def test_reconciliation_reports_a_committed_object_whose_bytes_are_gone(
    migrated: Engine,
    campaign: uuid.UUID,
    adapter: HerReplayAdapter,
    object_store: S3ObjectStore,
) -> None:
    """`missing` rather than `orphan`: a committed row promising bytes that are not there is the
    strongest loss signal the system has, and must not be filed beside merely unreferenced data."""
    work_item_id = _submit(migrated, campaign, adapter)
    absent_key = f"observations/{campaign}/{uuid.uuid4().hex}"
    uri = f"s3://{object_store.bucket}/{absent_key}"
    with migrated.begin() as connection:
        connection.execute(
            storage_objects.insert().values(
                object_uri=uri,
                bucket=object_store.bucket,
                object_key=absent_key,
                media_type="text/csv",
                work_item_id=work_item_id,
                sha256="e" * 64,
                byte_size=10,
                state="committed",
                committed_at=func.now(),
                created_at=func.now(),
            )
        )

    with migrated.begin() as connection:
        reconcile(connection, object_store)
        row = connection.execute(
            select(storage_objects).where(storage_objects.c.object_uri == uri)
        ).one()

    assert row.classification == "missing"
    assert "committed evidence" in row.classification_reason


def test_reconciliation_leaves_accepted_evidence_exactly_as_it_found_it(
    migrated: Engine,
    campaign: uuid.UUID,
    adapter: HerReplayAdapter,
    object_store: S3ObjectStore,
) -> None:
    """Released evidence is immutable. A reconciliation pass may label it, and may do nothing
    else — no rewrite, no deletion, no change of state."""
    work_item_id = _submit(migrated, campaign, adapter)
    outcome = _run(migrated, adapter, object_store, name="worker-accepting")
    assert outcome is not None
    assert outcome.status == "succeeded"
    with migrated.begin() as connection:
        before = connection.execute(
            select(storage_objects).where(
                storage_objects.c.work_item_id == work_item_id,
                storage_objects.c.state == "committed",
            )
        ).all()
    assert before

    with migrated.begin() as connection:
        reconcile(connection, object_store)
        after = connection.execute(
            select(storage_objects).where(
                storage_objects.c.work_item_id == work_item_id,
                storage_objects.c.state == "committed",
            )
        ).all()
        accepted = connection.execute(
            select(func.count())
            .select_from(observations)
            .where(
                observations.c.work_item_id == work_item_id,
                observations.c.status == "accepted",
            )
        ).scalar_one()

    assert len(after) == len(before)
    for row in after:
        assert row.classification == "accepted_evidence"
        assert row.state == "committed"
        assert object_store.exists(row.object_key)
    assert accepted == ONE
    matching = [row for row in after if row.object_uri in {b.object_uri for b in before}]
    for row in matching:
        original = next(b for b in before if b.object_uri == row.object_uri)
        assert row.sha256 == original.sha256
        assert row.byte_size == original.byte_size
        assert row.committed_at == original.committed_at


def test_duplicate_suppressed_bytes_are_retained_and_classified(
    migrated: Engine,
    campaign: uuid.UUID,
    adapter: HerReplayAdapter,
    object_store: S3ObjectStore,
) -> None:
    """A refused duplicate uploaded bytes. They are kept under its own attempt as a `received`
    receipt, and because the identity is content-derived, the fact that they matched the accepted
    bytes is recorded rather than assumed (invariant 2)."""
    work_item_id = _submit(migrated, campaign, adapter)
    first = _run(migrated, adapter, object_store, name="worker-first")
    assert first is not None
    assert first.status == "succeeded"

    with migrated.begin() as connection:
        context = connection.execute(
            select(events.c.event_id, events.c.correlation_id)
            .where(events.c.campaign_id == campaign, events.c.aggregate_id == work_item_id)
            .order_by(events.c.sequence.desc())
            .limit(1)
        ).one()
        enqueue(
            connection,
            campaign_id=campaign,
            work_item_id=work_item_id,
            instruction_key=f"redelivery:{uuid.uuid4().hex}",
            command_version="1",
            correlation_id=context.correlation_id,
            causation_id=context.event_id,
        )
    second = _run(migrated, adapter, object_store, name="worker-second")

    assert second is not None
    assert second.status == "duplicate_suppressed"
    with migrated.begin() as connection:
        reconcile(connection, object_store)
        receipts = connection.execute(
            select(
                observations.c.observation_id,
                observations.c.status,
                observations.c.sha256,
                observations.c.byte_size,
                observations.c.attempt_id,
                observations.c.status_reason,
            ).where(observations.c.work_item_id == work_item_id)
        ).all()
        suppressed = connection.execute(
            select(attempt_outcomes.c.observation_id).where(
                attempt_outcomes.c.work_item_id == work_item_id,
                attempt_outcomes.c.status == "duplicate_suppressed",
            )
        ).scalar_one()

    statuses = sorted(row.status for row in receipts)
    assert statuses == ["accepted", "received"]
    accepted_row = next(row for row in receipts if row.status == "accepted")
    received_row = next(row for row in receipts if row.status == "received")
    # Retained with its own digest, size and attempt identity — not assumed identical.
    assert received_row.sha256 == accepted_row.sha256
    assert received_row.byte_size == accepted_row.byte_size
    assert received_row.attempt_id != accepted_row.attempt_id
    assert received_row.status_reason
    # Content-derived identity, so the match is a fact in the table rather than an assumption.
    assert received_row.observation_id == accepted_row.observation_id
    assert suppressed == received_row.observation_id


def _run(
    engine: Engine, adapter: HerReplayAdapter, store: S3ObjectStore, *, name: str
) -> WorkOutcome | None:
    """One worker turn, on its own event loop. The lease is generous and the heartbeat fast, so a
    test that is not about expiry cannot fail on it."""
    worker = Worker(
        engine,
        adapter,
        store,
        name=name,
        fixture_seed=SPEC.seed,
        lease_seconds=30,
        heartbeat_seconds=FAST_HEARTBEAT_SECONDS,
    )
    return asyncio.run(worker.run_once())


def test_a_slow_upload_keeps_its_lease_because_the_worker_heartbeats(
    migrated: Engine,
    campaign: uuid.UUID,
    adapter: HerReplayAdapter,
    object_store: S3ObjectStore,
) -> None:
    """The heartbeat has to cover the upload, not just the adapter call.

    An upload that outlasts the lease would otherwise lose the job to a reclaim and have its result
    refused — correct, but a needless loss of work that was proceeding normally. The store here
    takes longer than the whole lease, and the run still succeeds.
    """
    work_item_id = _submit(migrated, campaign, adapter)

    class _SlowStore:
        def __init__(self, inner: S3ObjectStore) -> None:
            self._inner = inner
            self.bucket = inner.bucket

        def put_and_verify(self, key: str, data: bytes, *, media_type: str):  # type: ignore[no-untyped-def]
            time.sleep(SHORT_LEASE_SECONDS * 1.5)
            return self._inner.put_and_verify(key, data, media_type=media_type)

        def get(self, key: str) -> bytes:
            return self._inner.get(key)

        def exists(self, key: str) -> bool:
            return self._inner.exists(key)

    worker = Worker(
        migrated,
        adapter,
        _SlowStore(object_store),  # type: ignore[arg-type]
        name="worker-slow-upload",
        fixture_seed=SPEC.seed,
        lease_seconds=SHORT_LEASE_SECONDS,
        heartbeat_seconds=FAST_HEARTBEAT_SECONDS,
    )
    outcome = asyncio.run(worker.run_once())

    assert outcome is not None
    assert outcome.status == "succeeded"
    with migrated.begin() as connection:
        accepted = connection.execute(
            select(func.count())
            .select_from(observations)
            .where(
                observations.c.work_item_id == work_item_id,
                observations.c.status == "accepted",
            )
        ).scalar_one()
    assert accepted == ONE

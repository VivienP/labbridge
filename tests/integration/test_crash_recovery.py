"""What survives a worker dying mid-flight.

A process killed after an accepted outcome is committed does not lose that outcome after restart.
PO-03 states the same thing as a proof obligation.

The crash is injected where it actually hurts — between the object upload and the outcome
transaction. That is the window the worker's step ordering exists to make survivable: bytes are in
storage, nothing in the database references them, and the job is still leased by a process that will
never come back.

The worker runs as a **real subprocess** and is killed, because that is the only way to cross a
process boundary. An exception raised inside the worker is caught by the worker's own handlers and
recorded as a `failed_retryable` outcome — correct behaviour, and precisely why it proves nothing
about a process that stops existing (`AI_CONTRACT.md` §9).

No production code is modified to make this testable: the subprocess entry point in
`worker_subprocess.py` wraps the object store, and the store it wraps is the real one.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, func, select, text
from worker_subprocess import KILL_STAGES

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
from labbridge.runtime.jobs import claim, enqueue, expire_lease_now, recover_expired_leases
from labbridge.runtime.worker import Worker

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC = FixtureSpec(areas_per_library=6, seccm_areas_per_library=2)
ONE = 1
TWO_ATTEMPTS = 2


def kill_worker_at(
    stage: str,
    fixture_root: Path,
    tmp_path: Path,
    *,
    worker_name: str = "worker-subprocess",
    lease_seconds: int = 60,
    heartbeat_seconds: float = 5.0,
) -> dict[str, Any]:
    """Start a real worker process, let it reach one boundary, then kill it. Report what it saw.

    Killed rather than signalled: the point is a process that stops existing at a chosen point, so
    the state it leaves behind is the state recovery actually has to deal with. The returned mapping
    is what the child published before dying — identifiers, the lease it held, its fencing token,
    any staged object key, and whether the outcome transaction had committed.

    One harness for every boundary. A second fault framework would be a second thing to keep honest.
    """
    state_path = tmp_path / f"{stage}.json"
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("worker_subprocess.py")),
            str(fixture_root),
            str(state_path),
            stage,
        ],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "LABBRIDGE_WORKER_NAME": worker_name,
            "LABBRIDGE_LEASE_SECONDS": str(lease_seconds),
            "LABBRIDGE_HEARTBEAT_SECONDS": str(heartbeat_seconds),
            "PYTHONPATH": os.pathsep.join(
                (
                    str(REPO_ROOT / ".venv" / "Lib" / "site-packages"),
                    str(REPO_ROOT / "src"),
                    str(REPO_ROOT),
                )
            ),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            state = _read_state(state_path)
            if state.get("reached"):
                return state
            if process.poll() is not None:
                _, err = process.communicate()
                message = (
                    f"worker subprocess exited before reaching {stage}: "
                    f"{err.decode(errors='replace')}"
                )
                raise AssertionError(message)
            time.sleep(0.05)
        message = f"worker subprocess never reached {stage}"
        raise AssertionError(message)
    finally:
        process.kill()
        process.wait(timeout=30)


def _read_state(path: Path) -> dict[str, Any]:
    """Read the child's published state, tolerating the moment between write and rename."""
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


@pytest.fixture(scope="session")
def crash_fixture_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("crash-fixture")
    manifest = build_fixture(root, spec=SPEC, generator_version="0.1.0")
    write_document(root / FIXTURE_MANIFEST_FILENAME, manifest)
    return root


@pytest.fixture
def adapter(crash_fixture_root: Path) -> HerReplayAdapter:
    return HerReplayAdapter(crash_fixture_root)


@pytest.fixture
def campaign(
    migrated: Engine, purge_campaign: Callable[[Connection, uuid.UUID], None]
) -> Iterator[uuid.UUID]:
    campaign_id = uuid.uuid4()
    with migrated.begin() as connection:
        connection.execute(
            campaigns.insert().values(
                campaign_id=campaign_id,
                name="crash recovery",
                environment_id="her_auirrh",
                adapter_version="1",
                data_origin="synthetic",
                execution_mode="replay",
                state="active",
                declaration={},
                declaration_hash="c" * 64,
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
                "name": "crash recovery",
                "environment_id": "her_auirrh",
                "adapter_version": "1",
                "data_origin": "synthetic",
                "execution_mode": "replay",
                "declaration": {},
                "declaration_hash": "c" * 64,
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
    key = adapter.known_locations()[0]
    candidate = HerCandidate(
        library_id=key.library_id,
        measurement_area_id=key.measurement_area_id,
        grid_x=Quantity(value=Decimal("0"), unit="mm"),
        grid_y=Quantity(value=Decimal("0"), unit="mm"),
    )
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
        enqueue(
            connection,
            campaign_id=campaign_id,
            work_item_id=work_item_id,
            instruction_key=work_item_instruction_key(
                work_item_id=work_item_id, command_version="1"
            ),
            command_version="1",
            correlation_id=campaign_event.correlation_id,
            causation_id=queued_event.event_id,
        )
    return work_item_id


async def test_a_crash_after_upload_loses_no_outcome_and_creates_no_duplicate(
    migrated: Engine,
    adapter: HerReplayAdapter,
    object_store: S3ObjectStore,
    campaign: uuid.UUID,
    crash_fixture_root: Path,
    tmp_path: Path,
) -> None:
    """The first worker dies with the bytes uploaded and nothing in the
    database referencing them; after the lease lapses a second worker completes the work, and the
    campaign ends with exactly one accepted outcome."""
    work_item_id = _submit(migrated, campaign, adapter)
    kill_worker_at("after_upload_before_outcome_transaction", crash_fixture_root, tmp_path)

    # Nothing was accepted, and the job is still held by a process that no longer exists.
    with migrated.begin() as connection:
        assert _accepted(connection, work_item_id) == 0
        job_id = connection.execute(
            select(jobs.c.job_id).where(jobs.c.work_item_id == work_item_id)
        ).scalar_one()
        expire_lease_now(connection, job_id)
        assert len(recover_expired_leases(connection)) == ONE

    survivor = Worker(migrated, adapter, object_store, name="worker-survivor")
    outcome = await survivor.run_once()

    assert outcome is not None
    assert outcome.status == "succeeded"
    with migrated.begin() as connection:
        assert _accepted(connection, work_item_id) == ONE
        attempt_count = connection.execute(
            select(func.count())
            .select_from(attempts)
            .where(attempts.c.work_item_id == work_item_id)
        ).scalar_one()
    # Both attempts are recorded: the dead one and the one that finished. A retry is a new attempt,
    # never a rewrite of the previous one (docs/SPEC.md §7.3).
    assert attempt_count == TWO_ATTEMPTS


async def test_the_orphaned_object_is_visible_as_pending_rather_than_lost(
    migrated: Engine,
    adapter: HerReplayAdapter,
    object_store: S3ObjectStore,
    campaign: uuid.UUID,
    crash_fixture_root: Path,
    tmp_path: Path,
) -> None:
    """Why the upload comes first. The bytes are in storage and a `pending` row points at them, so a
    sweep can find the orphan. Had the row been written only after the upload, the object would be
    unreferenced by anything and unfindable without listing the whole bucket."""
    _submit(migrated, campaign, adapter)
    kill_worker_at("after_upload_before_outcome_transaction", crash_fixture_root, tmp_path)

    with migrated.begin() as connection:
        pending = connection.execute(
            select(storage_objects).where(storage_objects.c.state == "pending")
        ).all()
    assert pending
    for row in pending:
        # Pending, so no checksum is claimed yet: §4.2 forbids declaring an artifact committed
        # before its bytes are verified, and this row has not earned that yet.
        assert row.sha256 is None
        assert row.committed_at is None


async def test_a_committed_outcome_survives_and_is_not_reprocessed(
    migrated: Engine, adapter: HerReplayAdapter, object_store: S3ObjectStore, campaign: uuid.UUID
) -> None:
    """PO-03 from the other side: once the outcome is committed, a restart finds nothing to redo."""
    work_item_id = _submit(migrated, campaign, adapter)
    worker = Worker(migrated, adapter, object_store, name="worker-a")
    first = await worker.run_once()

    restarted = Worker(migrated, adapter, object_store, name="worker-b")
    second = await restarted.run_once()

    assert first is not None
    assert first.status == "succeeded"
    assert second is None
    with migrated.begin() as connection:
        assert _accepted(connection, work_item_id) == ONE


def _accepted(connection: Connection, work_item_id: uuid.UUID) -> int:
    return int(
        connection.execute(
            select(func.count())
            .select_from(attempt_outcomes)
            .where(
                attempt_outcomes.c.work_item_id == work_item_id,
                attempt_outcomes.c.status == "succeeded",
            )
        ).scalar_one()
    )


async def test_a_lease_lost_mid_flight_does_not_let_the_dead_worker_complete(
    migrated: Engine, adapter: HerReplayAdapter, object_store: S3ObjectStore, campaign: uuid.UUID
) -> None:
    """F-005/F-006. A worker whose lease was reclaimed must not mark the new owner's job done."""
    work_item_id = _submit(migrated, campaign, adapter)
    with migrated.begin() as connection:
        stale = claim(connection, owner="worker-paused")
        assert stale is not None
        expire_lease_now(connection, stale.job_id)
        recover_expired_leases(connection)

    survivor = Worker(migrated, adapter, object_store, name="worker-survivor")
    outcome = await survivor.run_once()

    assert outcome is not None
    assert outcome.status == "succeeded"
    with migrated.begin() as connection:
        assert _accepted(connection, work_item_id) == ONE


@pytest.mark.parametrize("stage", KILL_STAGES)
async def test_every_kill_stage_leaves_a_recoverable_state(
    migrated: Engine,
    adapter: HerReplayAdapter,
    object_store: S3ObjectStore,
    campaign: uuid.UUID,
    crash_fixture_root: Path,
    tmp_path: Path,
    stage: str,
) -> None:
    """One real process killed at each durable boundary, then recovered by the same reconciliation
    a worker runs at startup.

    Whatever the boundary, the campaign ends with exactly one accepted observation and no attempt
    left `running`. The differences between the stages are in *what recovery had to do*, not in the
    result — which is the property the whole design is for.
    """
    work_item_id = _submit(migrated, campaign, adapter)
    with _only_claimable(migrated, work_item_id):
        state = kill_worker_at(
            stage, crash_fixture_root, tmp_path, worker_name=f"worker-{stage}", lease_seconds=2
        )

    assert state["kill_stage"] == stage, state
    assert state["pid"], state
    assert state.get("job_id"), state
    assert state.get("fencing_token", 0) >= ONE, sorted(state.items())
    assert state.get("lease_owner") == f"worker-{stage}", state
    # Only the last stage got as far as a committed outcome.
    assert state["committed"] is (stage == "after_commit_before_acknowledgement"), state
    if stage in {"after_upload_before_outcome_transaction", "after_commit_before_acknowledgement"}:
        assert state["object_keys"]
        assert object_store.exists(state["object_keys"][0])

    # Recovery, exactly as a restarting worker performs it. The wait is real rather than mocked:
    # the lease expiry is evaluated by the database, so it has to actually elapse.
    await asyncio.sleep(2.5)
    survivor = Worker(migrated, adapter, object_store, name="worker-survivor", lease_seconds=30)
    report = survivor.start()
    # Drained rather than run once: a claim takes whatever job is available, and the suite leaves
    # other campaigns' work in the queue. Stopping when *this* work item is resolved keeps the
    # proof about the boundary under test rather than about queue order.
    outcomes = await _drain_until_resolved(survivor, migrated, work_item_id)

    with migrated.begin() as connection:
        accepted = connection.execute(
            select(func.count())
            .select_from(observations)
            .where(
                observations.c.work_item_id == work_item_id,
                observations.c.status == "accepted",
            )
        ).scalar_one()
        running = connection.execute(
            select(func.count())
            .select_from(attempts)
            .where(attempts.c.work_item_id == work_item_id, attempts.c.state == "running")
        ).scalar_one()
        succeeded = connection.execute(
            select(func.count())
            .select_from(attempt_outcomes)
            .where(
                attempt_outcomes.c.work_item_id == work_item_id,
                attempt_outcomes.c.status == "succeeded",
            )
        ).scalar_one()

    # Exactly one accepted observation, whichever boundary the process died at: none lost, none
    # duplicated (PO-02, PO-03).
    assert accepted == ONE
    assert succeeded == ONE
    # No known failure leaves an attempt indefinitely in flight.
    assert running == 0

    if stage == "after_commit_before_acknowledgement":
        # The work was already durable before the process died. Nothing was re-executed for this
        # work item, and the accepted outcome the dead worker committed is still the authoritative
        # one — the acknowledgement was all that was lost.
        assert state["outcome_status"] == "succeeded"
        assert state["observation_id"]
    else:
        # The reclaim is what made the work available again, and the survivor is what accepted it.
        assert len(report.reclaimed) >= ONE
        assert "succeeded" in outcomes


async def test_a_late_result_after_lease_loss_is_diagnostic_only(
    migrated: Engine,
    adapter: HerReplayAdapter,
    object_store: S3ObjectStore,
    campaign: uuid.UUID,
) -> None:
    """F-008. The adapter returns and the bytes land, but the lease was reclaimed while it ran.

    The result is refused from accepted state and the bytes are kept as diagnostic evidence — a
    late result is the strongest evidence there is that a worker was still running when its lease
    was taken away, and discarding it would erase exactly that.
    """
    work_item_id = _submit(migrated, campaign, adapter)

    class _ReclaimDuringUpload:
        """Takes the lease away between the adapter returning and the outcome transaction."""

        def __init__(self, inner: S3ObjectStore) -> None:
            self._inner = inner
            self.bucket = inner.bucket

        def put_and_verify(self, key: str, data: bytes, *, media_type: str):  # type: ignore[no-untyped-def]
            stored = self._inner.put_and_verify(key, data, media_type=media_type)
            with migrated.begin() as connection:
                job_id = connection.execute(
                    select(jobs.c.job_id).where(jobs.c.work_item_id == work_item_id)
                ).scalar_one()
                expire_lease_now(connection, job_id)
                recover_expired_leases(connection)
            return stored

        def get(self, key: str) -> bytes:
            return self._inner.get(key)

        def exists(self, key: str) -> bool:
            return self._inner.exists(key)

    worker = Worker(
        migrated,
        adapter,
        _ReclaimDuringUpload(object_store),  # type: ignore[arg-type]
        name="worker-late",
        lease_seconds=30,
    )
    outcome = await worker.run_once()

    assert outcome is not None
    assert outcome.status == "lease_lost"
    with migrated.begin() as connection:
        receipts = connection.execute(
            select(
                observations.c.status, observations.c.status_reason, observations.c.sha256
            ).where(observations.c.work_item_id == work_item_id)
        ).all()
        recorded = connection.execute(
            select(attempt_outcomes.c.status, attempt_outcomes.c.failure).where(
                attempt_outcomes.c.work_item_id == work_item_id
            )
        ).all()
        accepted_events = connection.execute(
            select(func.count())
            .select_from(events)
            .where(
                events.c.campaign_id == campaign,
                events.c.event_type == "observation.accepted",
            )
        ).scalar_one()

    # Bytes kept, and kept as a receipt rather than as a result.
    assert [row.status for row in receipts] == ["received"]
    assert receipts[0].sha256
    assert "no longer held the job" in receipts[0].status_reason
    # No accepted observation, no acceptance event.
    assert accepted_events == 0
    assert [row.status for row in recorded] == ["lease_lost"]
    assert recorded[0].failure["failure_code"] == "lease_lost"
    assert "fencing token" in recorded[0].failure["summary"]


async def _drain_until_resolved(
    worker: Worker, engine: Engine, work_item_id: uuid.UUID, *, limit: int = 10
) -> list[str]:
    """Run the worker until this work item has an accepted observation, or the queue empties.

    Returns the statuses it produced along the way. Bounded, so a work item that can never be
    accepted fails the calling assertion rather than hanging the suite.
    """
    statuses: list[str] = []
    for _ in range(limit):
        with engine.begin() as connection:
            resolved = connection.execute(
                select(func.count())
                .select_from(observations)
                .where(
                    observations.c.work_item_id == work_item_id,
                    observations.c.status == "accepted",
                )
            ).scalar_one()
        if resolved:
            return statuses
        outcome = await worker.run_once()
        if outcome is None:
            return statuses
        statuses.append(outcome.status)
    return statuses


@contextmanager
def _only_claimable(engine: Engine, work_item_id: uuid.UUID) -> Iterator[None]:
    """Make this work item the only job a worker can claim, then put the queue back.

    A claim takes whatever is available, and this suite leaves other campaigns' work in the queue.
    Without this the subprocess under test can pick up an unrelated job and never reach the boundary
    the test is about — a flake that looks like a recovery bug.

    Deferred rather than cancelled: the other jobs keep their state and become claimable again
    afterwards, so isolating one test cannot quietly destroy another's fixture.
    """
    with engine.begin() as connection:
        deferred = (
            connection.execute(
                select(jobs.c.job_id).where(
                    jobs.c.state == "available", jobs.c.work_item_id != work_item_id
                )
            )
            .scalars()
            .all()
        )
        if deferred:
            connection.execute(
                jobs.update()
                .where(jobs.c.job_id.in_(deferred))
                .values(available_at=func.now() + text("interval '1 hour'"))
            )
    try:
        yield
    finally:
        with engine.begin() as connection:
            if deferred:
                connection.execute(
                    jobs.update().where(jobs.c.job_id.in_(deferred)).values(available_at=func.now())
                )

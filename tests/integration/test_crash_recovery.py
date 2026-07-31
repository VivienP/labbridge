"""What survives a worker dying mid-flight.

`docs/ROADMAP.md` Slice 1 exit criterion: *a process killed after an accepted outcome is committed
does not lose that outcome after restart*. PO-03 states the same thing as a proof obligation.

The crash is injected where it actually hurts — between the object upload and the outcome
transaction. That is the window the worker's step ordering exists to make survivable: bytes are in
storage, nothing in the database references them, and the job is still leased by a process that will
never come back.

No production code is modified to make this testable. The crash comes from a store wrapper that
raises after a successful upload, which is exactly what a process death after `put_and_verify` looks
like from the database's point of view.
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
from labbridge.infrastructure.objectstore import S3ObjectStore, StoredObject
from labbridge.infrastructure.persistence.tables import (
    attempt_outcomes,
    attempts,
    campaigns,
    jobs,
    storage_objects,
    work_items,
)
from labbridge.runtime.jobs import claim, enqueue, expire_lease_now, recover_expired_leases
from labbridge.runtime.worker import Worker

pytestmark = pytest.mark.integration

SPEC = FixtureSpec(areas_per_library=6, seccm_areas_per_library=2)
ONE = 1
TWO_ATTEMPTS = 2


class CrashAfterUpload:
    """A store that writes the object and then the process dies.

    Delegates rather than fakes: the bytes really are uploaded and really are verified, so the
    database is left in exactly the state a mid-flight death leaves it in.
    """

    def __init__(self, inner: S3ObjectStore) -> None:
        self._inner = inner
        self.bucket = inner.bucket
        self.uploads = 0

    def put_and_verify(self, key: str, data: bytes, *, media_type: str) -> StoredObject:
        self._inner.put_and_verify(key, data, media_type=media_type)
        self.uploads += 1
        message = "worker process died after the upload, before the outcome transaction"
        raise RuntimeError(message)

    def get(self, key: str) -> bytes:
        return self._inner.get(key)

    def exists(self, key: str) -> bool:
        return self._inner.exists(key)


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
                created_at=func.now(),
                updated_at=func.now(),
            )
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
        enqueue(
            connection,
            work_item_id=work_item_id,
            idempotency_key=f"crash:{uuid.uuid4().hex}",
            command_version="1",
        )
    return work_item_id


async def test_a_crash_after_upload_loses_no_outcome_and_creates_no_duplicate(
    migrated: Engine, adapter: HerReplayAdapter, object_store: S3ObjectStore, campaign: uuid.UUID
) -> None:
    """The Slice 1 exit criterion. The first worker dies with the bytes uploaded and nothing in the
    database referencing them; after the lease lapses a second worker completes the work, and the
    campaign ends with exactly one accepted outcome."""
    work_item_id = _submit(migrated, campaign, adapter)
    crashing = Worker(migrated, adapter, CrashAfterUpload(object_store), name="worker-doomed")

    with pytest.raises(RuntimeError, match="died after the upload"):
        await crashing.run_once()

    # Nothing was accepted, and the job is still held by a worker that will never return.
    with migrated.begin() as connection:
        assert _accepted(connection, work_item_id) == 0
        job_id = connection.execute(
            select(jobs.c.job_id).where(jobs.c.work_item_id == work_item_id)
        ).scalar_one()
        expire_lease_now(connection, job_id)
        assert recover_expired_leases(connection) == 1

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
    migrated: Engine, adapter: HerReplayAdapter, object_store: S3ObjectStore, campaign: uuid.UUID
) -> None:
    """Why the upload comes first. The bytes are in storage and a `pending` row points at them, so a
    sweep can find the orphan. Had the row been written only after the upload, the object would be
    unreferenced by anything and unfindable without listing the whole bucket."""
    _submit(migrated, campaign, adapter)
    crashing = Worker(migrated, adapter, CrashAfterUpload(object_store), name="worker-doomed")

    with pytest.raises(RuntimeError):
        await crashing.run_once()

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

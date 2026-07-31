"""Durable jobs against real PostgreSQL.

There is no offline version of this file on purpose. Every property here — atomic claim, lease
expiry, stale-token refusal — is a property of the database's concurrency control, and a test that
substituted an in-memory queue would prove something about the substitute
(`AI_CONTRACT.md` §9, `docs/SPEC.md` §15).

Concurrency is exercised with two *separate connections*, because two statements on one connection
share a transaction and can never contend. That distinction is the difference between testing
`SKIP LOCKED` and testing nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, Engine, delete, func, select

from labbridge.infrastructure.persistence.tables import campaigns, jobs, work_items
from labbridge.runtime.jobs import (
    LeaseLostError,
    claim,
    complete,
    enqueue,
    expire_lease_now,
    fail_terminally,
    heartbeat,
    mark_running,
    recover_expired_leases,
    schedule_retry,
)

pytestmark = pytest.mark.integration

ATTEMPTS_ALLOWED = 3
SECOND_ATTEMPT = 2


@pytest.fixture
def work_item(connection: Connection) -> uuid.UUID:
    """A committed work item, so the other connections in this module can see it."""
    campaign_id = uuid.uuid4()
    work_item_id = uuid.uuid4()
    connection.execute(
        campaigns.insert().values(
            campaign_id=campaign_id,
            name="job tests",
            environment_id="her",
            adapter_version="1",
            data_origin="synthetic",
            execution_mode="replay",
            state="active",
            declaration={},
            declaration_hash="d" * 64,
            created_at=func.now(),
            updated_at=func.now(),
        )
    )
    connection.execute(
        work_items.insert().values(
            work_item_id=work_item_id,
            campaign_id=campaign_id,
            candidate_id=f"cand:{uuid.uuid4().hex}",
            candidate={},
            state="queued",
            created_at=func.now(),
            updated_at=func.now(),
        )
    )
    return work_item_id


@pytest.fixture
def committed_work_item(migrated: Engine) -> Iterator[uuid.UUID]:
    """Committed rather than rolled back: the concurrency tests need other connections to see it."""
    campaign_id = uuid.uuid4()
    work_item_id = uuid.uuid4()
    with migrated.begin() as connection:
        connection.execute(
            campaigns.insert().values(
                campaign_id=campaign_id,
                name="concurrent job tests",
                environment_id="her",
                adapter_version="1",
                data_origin="synthetic",
                execution_mode="replay",
                state="active",
                declaration={},
                declaration_hash="e" * 64,
                created_at=func.now(),
                updated_at=func.now(),
            )
        )
        connection.execute(
            work_items.insert().values(
                work_item_id=work_item_id,
                campaign_id=campaign_id,
                candidate_id=f"cand:{uuid.uuid4().hex}",
                candidate={},
                state="queued",
                created_at=func.now(),
                updated_at=func.now(),
            )
        )
    yield work_item_id
    with migrated.begin() as connection:
        connection.execute(delete(jobs).where(jobs.c.work_item_id == work_item_id))
        connection.execute(delete(work_items).where(work_items.c.work_item_id == work_item_id))
        connection.execute(delete(campaigns).where(campaigns.c.campaign_id == campaign_id))


def _enqueue(connection: Connection, work_item_id: uuid.UUID, **kwargs: object) -> uuid.UUID:
    job_id = enqueue(
        connection,
        work_item_id=work_item_id,
        idempotency_key=f"key:{uuid.uuid4().hex}",
        command_version="1",
        **kwargs,  # type: ignore[arg-type]
    )
    assert job_id is not None
    return job_id


def test_a_duplicate_idempotency_key_does_not_create_a_second_job(
    connection: Connection, work_item: uuid.UUID
) -> None:
    """F-001: a resubmitted request is the same job, not a second one."""
    key = f"key:{uuid.uuid4().hex}"
    first = enqueue(connection, work_item_id=work_item, idempotency_key=key, command_version="1")
    second = enqueue(connection, work_item_id=work_item, idempotency_key=key, command_version="1")

    assert first is not None
    assert second is None


def test_claiming_marks_the_job_leased_and_counts_the_attempt(
    connection: Connection, work_item: uuid.UUID
) -> None:
    job_id = _enqueue(connection, work_item)

    lease = claim(connection, owner="worker-a")

    assert lease is not None
    assert lease.job_id == job_id
    assert lease.attempt_count == 1
    state = connection.execute(select(jobs.c.state).where(jobs.c.job_id == job_id)).scalar_one()
    assert state == "leased"


def test_an_empty_queue_returns_none_rather_than_blocking(connection: Connection) -> None:
    """Cancelled rather than deleted: attempts reference jobs under RESTRICT, so deleting the queue
    would test the teardown rather than the claim."""
    connection.execute(jobs.update().where(jobs.c.state == "available").values(state="cancelled"))

    assert claim(connection, owner="worker-a") is None


def test_two_concurrent_workers_never_claim_the_same_job(
    migrated: Engine, committed_work_item: uuid.UUID
) -> None:
    """The property `SKIP LOCKED` exists for. Two separate connections, so the claims genuinely
    contend; on one connection they would share a transaction and could not."""
    with migrated.begin() as setup:
        _enqueue(setup, committed_work_item)

    first_connection = migrated.connect()
    second_connection = migrated.connect()
    try:
        first_txn = first_connection.begin()
        second_txn = second_connection.begin()
        first = claim(first_connection, owner="worker-a")
        second = claim(second_connection, owner="worker-b")
        first_txn.rollback()
        second_txn.rollback()
    finally:
        first_connection.close()
        second_connection.close()

    claimed = [lease for lease in (first, second) if lease is not None]
    assert len(claimed) == 1


def test_a_heartbeat_extends_the_lease(connection: Connection, work_item: uuid.UUID) -> None:
    _enqueue(connection, work_item)
    lease = claim(connection, owner="worker-a", lease_seconds=30)
    assert lease is not None

    extended = heartbeat(connection, lease, lease_seconds=120)

    assert extended > lease.lease_expires_at


def test_a_heartbeat_on_a_reclaimed_job_is_refused(
    connection: Connection, work_item: uuid.UUID
) -> None:
    """F-005: a worker that lost its lease must learn it, not carry on believing it owns the job."""
    job_id = _enqueue(connection, work_item)
    lease = claim(connection, owner="worker-a")
    assert lease is not None
    expire_lease_now(connection, job_id)
    recover_expired_leases(connection)
    stolen = claim(connection, owner="worker-b")
    assert stolen is not None

    with pytest.raises(LeaseLostError):
        heartbeat(connection, lease)


def test_a_completion_from_a_stale_lease_is_refused(
    connection: Connection, work_item: uuid.UUID
) -> None:
    """Expiry alone would not stop this: the paused worker still holds a token, and without the
    token check it would mark the new owner's job done for work nobody committed."""
    job_id = _enqueue(connection, work_item)
    stale = claim(connection, owner="worker-a")
    assert stale is not None
    expire_lease_now(connection, job_id)
    recover_expired_leases(connection)
    fresh = claim(connection, owner="worker-b")
    assert fresh is not None

    with pytest.raises(LeaseLostError):
        complete(connection, stale)

    complete(connection, fresh)
    state = connection.execute(select(jobs.c.state).where(jobs.c.job_id == job_id)).scalar_one()
    assert state == "succeeded"


def test_an_expired_lease_returns_the_job_to_the_queue(
    connection: Connection, work_item: uuid.UUID
) -> None:
    """A killed worker strands nothing: the job it held becomes claimable once the lease lapses."""
    job_id = _enqueue(connection, work_item)
    claim(connection, owner="worker-a")
    expire_lease_now(connection, job_id)

    recovered = recover_expired_leases(connection)

    assert recovered == 1
    reclaimed = claim(connection, owner="worker-b")
    assert reclaimed is not None
    assert reclaimed.job_id == job_id
    assert reclaimed.attempt_count == SECOND_ATTEMPT


def test_a_live_lease_is_not_recovered(connection: Connection, work_item: uuid.UUID) -> None:
    """Recovery that reclaimed live leases would hand running work to a second worker."""
    _enqueue(connection, work_item)
    claim(connection, owner="worker-a", lease_seconds=300)

    assert recover_expired_leases(connection) == 0


def test_recovery_fails_a_job_that_has_exhausted_its_attempts(
    connection: Connection, work_item: uuid.UUID
) -> None:
    """Otherwise a worker that dies every time loops forever, and the queue never drains."""
    job_id = _enqueue(connection, work_item, max_attempts=1)
    claim(connection, owner="worker-a")
    expire_lease_now(connection, job_id)

    recover_expired_leases(connection)

    state = connection.execute(select(jobs.c.state).where(jobs.c.job_id == job_id)).scalar_one()
    assert state == "failed_terminal"


def test_a_retry_becomes_available_later_not_immediately(
    connection: Connection, work_item: uuid.UUID
) -> None:
    """Backoff applied by the database, so a worker's clock cannot shorten it into a retry storm."""
    job_id = _enqueue(connection, work_item)
    lease = claim(connection, owner="worker-a")
    assert lease is not None

    available_at = schedule_retry(connection, lease, failure={"failure_code": "timeout"})

    assert available_at is not None
    row = connection.execute(
        select(jobs.c.state, jobs.c.last_failure).where(jobs.c.job_id == job_id)
    ).one()
    assert row.state == "available"
    assert row.last_failure["failure_code"] == "timeout"
    # Not claimable yet: the delay is in the future.
    assert claim(connection, owner="worker-b") is None


def test_a_retry_past_the_attempt_limit_fails_terminally(
    connection: Connection, work_item: uuid.UUID
) -> None:
    job_id = _enqueue(connection, work_item, max_attempts=1)
    lease = claim(connection, owner="worker-a")
    assert lease is not None
    assert lease.attempts_exhausted

    assert schedule_retry(connection, lease) is None

    state = connection.execute(select(jobs.c.state).where(jobs.c.job_id == job_id)).scalar_one()
    assert state == "failed_terminal"


def test_a_terminal_failure_is_not_retried_whatever_the_attempt_count(
    connection: Connection, work_item: uuid.UUID
) -> None:
    """An unsupported schema is not going to parse on the second try (F-019)."""
    job_id = _enqueue(connection, work_item, max_attempts=ATTEMPTS_ALLOWED)
    lease = claim(connection, owner="worker-a")
    assert lease is not None

    fail_terminally(connection, lease, failure={"failure_code": "unsupported_schema"})

    row = connection.execute(
        select(jobs.c.state, jobs.c.lease_token).where(jobs.c.job_id == job_id)
    ).one()
    assert row.state == "failed_terminal"
    assert row.lease_token is None
    assert claim(connection, owner="worker-b") is None


def test_completing_releases_the_lease(connection: Connection, work_item: uuid.UUID) -> None:
    job_id = _enqueue(connection, work_item)
    lease = claim(connection, owner="worker-a")
    assert lease is not None
    mark_running(connection, lease)

    complete(connection, lease)

    row = connection.execute(
        select(jobs.c.state, jobs.c.lease_owner, jobs.c.lease_expires_at).where(
            jobs.c.job_id == job_id
        )
    ).one()
    assert row.state == "succeeded"
    assert row.lease_owner is None
    assert row.lease_expires_at is None


def test_two_concurrent_workers_each_claim_a_different_job(
    migrated: Engine, committed_work_item: uuid.UUID
) -> None:
    """The stronger property: with two jobs available, both workers make progress and take
    *different* rows. Without SKIP LOCKED the second claim would block on the first's row lock
    instead of moving past it, so this is what separates the pattern from a plain FOR UPDATE."""
    with migrated.begin() as setup:
        first_job = _enqueue(setup, committed_work_item)
        second_job = _enqueue(setup, committed_work_item)

    first_connection = migrated.connect()
    second_connection = migrated.connect()
    try:
        first_txn = first_connection.begin()
        second_txn = second_connection.begin()
        first = claim(first_connection, owner="worker-a")
        second = claim(second_connection, owner="worker-b")
        first_txn.rollback()
        second_txn.rollback()
    finally:
        first_connection.close()
        second_connection.close()

    assert first is not None
    assert second is not None
    assert first.job_id != second.job_id
    assert {first.job_id, second.job_id} == {first_job, second_job}

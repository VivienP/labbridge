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
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, delete, func, select

from labbridge.domain.idempotency import InstructionConflictError
from labbridge.infrastructure.persistence.tables import campaigns, events, jobs, work_items
from labbridge.runtime.events import append_event
from labbridge.runtime.jobs import (
    EnqueuedJob,
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
ONE_JOB = 1
ONE_EVENT = 1
CONCURRENT_ENQUEUERS = 4


def _candidate_payload() -> dict[str, object]:
    return {
        "kind": "her_location",
        "library_id": "library",
        "measurement_area_id": "area",
        "grid_x": {"value": "0", "unit": "mm"},
        "grid_y": {"value": "0", "unit": "mm"},
    }


def _seed_events(
    connection: Connection,
    campaign_id: uuid.UUID,
    work_item_id: uuid.UUID,
    declaration_hash: str,
    *,
    name: str,
) -> None:
    correlation_id = uuid.uuid4()
    root = append_event(
        connection,
        campaign_id=campaign_id,
        aggregate_id=campaign_id,
        aggregate_type="campaign",
        event_type="campaign.created",
        payload={
            "name": name,
            "environment_id": "her",
            "adapter_version": "1",
            "data_origin": "synthetic",
            "execution_mode": "replay",
            "declaration": {},
            "declaration_hash": declaration_hash,
            "state": "active",
        },
        expected_version=0,
        correlation_id=correlation_id,
        causation_id=None,
    )
    append_event(
        connection,
        campaign_id=campaign_id,
        aggregate_id=work_item_id,
        aggregate_type="work_item",
        event_type="work_item.queued",
        payload={"candidate_id": "cand:test", "candidate": _candidate_payload(), "state": "queued"},
        expected_version=0,
        correlation_id=correlation_id,
        causation_id=root.event_id,
    )


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
            event_stream_contract_version=1,
            event_stream_last_position=0,
            created_at=func.now(),
            updated_at=func.now(),
        )
    )
    connection.execute(
        work_items.insert().values(
            work_item_id=work_item_id,
            campaign_id=campaign_id,
            candidate_id=f"cand:{uuid.uuid4().hex}",
            candidate=_candidate_payload(),
            state="queued",
            created_at=func.now(),
            updated_at=func.now(),
        )
    )
    _seed_events(connection, campaign_id, work_item_id, "d" * 64, name="job tests")
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
                event_stream_contract_version=1,
                event_stream_last_position=0,
                created_at=func.now(),
                updated_at=func.now(),
            )
        )
        connection.execute(
            work_items.insert().values(
                work_item_id=work_item_id,
                campaign_id=campaign_id,
                candidate_id=f"cand:{uuid.uuid4().hex}",
                candidate=_candidate_payload(),
                state="queued",
                created_at=func.now(),
                updated_at=func.now(),
            )
        )
        _seed_events(
            connection,
            campaign_id,
            work_item_id,
            "e" * 64,
            name="concurrent job tests",
        )
    yield work_item_id
    with migrated.begin() as connection:
        connection.execute(delete(jobs).where(jobs.c.work_item_id == work_item_id))
        connection.execute(delete(events).where(events.c.campaign_id == campaign_id))
        connection.execute(delete(work_items).where(work_items.c.work_item_id == work_item_id))
        connection.execute(delete(campaigns).where(campaigns.c.campaign_id == campaign_id))


def _job_context(connection: Connection, work_item_id: uuid.UUID) -> Any:
    return connection.execute(
        select(
            work_items.c.campaign_id,
            events.c.correlation_id,
            events.c.event_id,
        )
        .select_from(work_items.join(events, events.c.aggregate_id == work_items.c.work_item_id))
        .where(work_items.c.work_item_id == work_item_id, events.c.event_type == "work_item.queued")
    ).one()


def _enqueue(connection: Connection, work_item_id: uuid.UUID, **kwargs: object) -> uuid.UUID:
    instruction_key = str(kwargs.pop("instruction_key", f"key:{uuid.uuid4().hex}"))
    context = _job_context(connection, work_item_id)
    enqueued = enqueue(
        connection,
        campaign_id=context.campaign_id,
        work_item_id=work_item_id,
        instruction_key=instruction_key,
        command_version="1",
        correlation_id=context.correlation_id,
        causation_id=context.event_id,
        **kwargs,  # type: ignore[arg-type]
    )
    assert enqueued.created
    return enqueued.job_id


def test_a_duplicate_instruction_key_does_not_create_a_second_job(
    connection: Connection, work_item: uuid.UUID
) -> None:
    """F-001: a resubmitted instruction is the same job, not a second one."""
    key = f"key:{uuid.uuid4().hex}"
    context = _job_context(connection, work_item)
    first = _enqueue(connection, work_item, instruction_key=key)
    second = enqueue(
        connection,
        campaign_id=context.campaign_id,
        work_item_id=work_item,
        instruction_key=key,
        command_version="1",
        correlation_id=uuid.uuid4(),
        causation_id=uuid.uuid4(),
    )

    assert second.created is False
    assert second.job_id == first
    # The repeat produced no second job and no second `job.enqueued` event, so nothing downstream
    # can execute the instruction twice.
    assert (
        connection.execute(
            select(func.count()).select_from(jobs).where(jobs.c.work_item_id == work_item)
        ).scalar_one()
        == ONE_JOB
    )
    assert (
        connection.execute(
            select(func.count())
            .select_from(events)
            .where(events.c.aggregate_id == first, events.c.event_type == "job.enqueued")
        ).scalar_one()
        == ONE_EVENT
    )


def test_one_instruction_key_cannot_name_two_work_items(
    connection: Connection, work_item: uuid.UUID
) -> None:
    """A caller that builds its own key rather than deriving it gets a typed error, not a job that
    silently belongs to somebody else's work item."""
    key = f"key:{uuid.uuid4().hex}"
    context = _job_context(connection, work_item)
    _enqueue(connection, work_item, instruction_key=key)
    other_work_item = uuid.uuid4()
    connection.execute(
        work_items.insert().values(
            work_item_id=other_work_item,
            campaign_id=context.campaign_id,
            candidate_id=f"cand:{uuid.uuid4().hex}",
            candidate=_candidate_payload(),
            state="queued",
            created_at=func.now(),
            updated_at=func.now(),
        )
    )

    with pytest.raises(InstructionConflictError) as raised:
        enqueue(
            connection,
            campaign_id=context.campaign_id,
            work_item_id=other_work_item,
            instruction_key=key,
            command_version="1",
            correlation_id=context.correlation_id,
            causation_id=context.event_id,
        )

    assert raised.value.code == "instruction_key_reused"


def test_concurrent_duplicate_enqueues_create_exactly_one_job(
    migrated: Engine, committed_work_item: uuid.UUID
) -> None:
    """The constraint decides, not a prior read.

    Four separate connections submit the same logical instruction at once. A check-then-insert lets
    all four past the check, and three of them then collide in the index — an integrity error the
    caller never asked for. Here exactly one insert wins and the other three read what it wrote.
    """
    key = f"key:{uuid.uuid4().hex}"
    with migrated.begin() as setup:
        context = _job_context(setup, committed_work_item)
    barrier = Barrier(CONCURRENT_ENQUEUERS)

    def enqueue_once() -> EnqueuedJob:
        barrier.wait(timeout=30)
        with migrated.begin() as connection:
            return enqueue(
                connection,
                campaign_id=context.campaign_id,
                work_item_id=committed_work_item,
                instruction_key=key,
                command_version="1",
                correlation_id=context.correlation_id,
                causation_id=context.event_id,
            )

    with ThreadPoolExecutor(max_workers=CONCURRENT_ENQUEUERS) as pool:
        futures = [pool.submit(enqueue_once) for _ in range(CONCURRENT_ENQUEUERS)]
        results = [future.result(timeout=60) for future in futures]

    assert sum(1 for result in results if result.created) == ONE_JOB
    assert len({result.job_id for result in results}) == ONE_JOB
    with migrated.begin() as connection:
        assert (
            connection.execute(
                select(func.count()).select_from(jobs).where(jobs.c.idempotency_key == key)
            ).scalar_one()
            == ONE_JOB
        )
        assert (
            connection.execute(
                select(func.count())
                .select_from(events)
                .where(
                    events.c.aggregate_id == results[0].job_id,
                    events.c.event_type == "job.enqueued",
                )
            ).scalar_one()
            == ONE_EVENT
        )


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


def test_claim_skips_legacy_jobs_without_starving_complete_streams(
    connection: Connection, work_item: uuid.UUID
) -> None:
    legacy_campaign_id = uuid.uuid4()
    legacy_work_item_id = uuid.uuid4()
    legacy_job_id = uuid.uuid4()
    connection.execute(
        campaigns.insert().values(
            campaign_id=legacy_campaign_id,
            name="legacy job",
            environment_id="her",
            adapter_version="1",
            data_origin="synthetic",
            execution_mode="replay",
            state="active",
            declaration={},
            declaration_hash="0" * 64,
            event_stream_contract_version=0,
            event_stream_last_position=0,
            created_at=func.now(),
            updated_at=func.now(),
        )
    )
    connection.execute(
        work_items.insert().values(
            work_item_id=legacy_work_item_id,
            campaign_id=legacy_campaign_id,
            candidate_id="cand:legacy",
            candidate=_candidate_payload(),
            state="queued",
            created_at=func.now(),
            updated_at=func.now(),
        )
    )
    connection.execute(
        jobs.insert().values(
            job_id=legacy_job_id,
            work_item_id=legacy_work_item_id,
            state="available",
            available_at=func.now(),
            max_attempts=3,
            command_version="1",
            idempotency_key=f"legacy:{uuid.uuid4().hex}",
            created_at=func.now(),
            updated_at=func.now(),
        )
    )
    complete_job_id = _enqueue(connection, work_item)

    lease = claim(connection, owner="worker-a")

    assert lease is not None
    assert lease.job_id == complete_job_id


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

    def claim_and_commit(owner: str):
        with migrated.begin() as connection:
            return claim(connection, owner=owner)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(claim_and_commit, ("worker-a", "worker-b"))

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

    assert len(recovered) == ONE_JOB
    reclaimed = claim(connection, owner="worker-b")
    assert reclaimed is not None
    assert reclaimed.job_id == job_id
    assert reclaimed.attempt_count == SECOND_ATTEMPT


def test_a_live_lease_is_not_recovered(connection: Connection, work_item: uuid.UUID) -> None:
    """Recovery that reclaimed live leases would hand running work to a second worker."""
    _enqueue(connection, work_item)
    claim(connection, owner="worker-a", lease_seconds=300)

    assert recover_expired_leases(connection) == []


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


def test_two_workers_each_claim_a_different_job(
    migrated: Engine, committed_work_item: uuid.UUID
) -> None:
    """With two jobs available, two concurrent claims take different rows."""
    with migrated.begin() as setup:
        first_job = _enqueue(setup, committed_work_item)
        second_job = _enqueue(setup, committed_work_item)
    barrier = Barrier(2)

    def claim_and_commit(owner: str):
        barrier.wait()
        connection = migrated.connect()
        transaction = connection.begin()
        try:
            lease = claim(connection, owner=owner)
            transaction.commit()
            return lease
        finally:
            if transaction.is_active:
                transaction.rollback()
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim_and_commit, owner) for owner in ("worker-a", "worker-b")]
        first, second = (future.result(timeout=10) for future in futures)

    assert first is not None
    assert second is not None
    assert first.job_id != second.job_id
    assert {first.job_id, second.job_id} == {first_job, second_job}


def test_recovery_ignores_legacy_jobs_and_recovers_complete_jobs(
    connection: Connection, work_item: uuid.UUID
) -> None:
    legacy_campaign_id = uuid.uuid4()
    legacy_work_item_id = uuid.uuid4()
    legacy_job_id = uuid.uuid4()
    connection.execute(
        campaigns.insert().values(
            campaign_id=legacy_campaign_id,
            name="legacy recovery",
            environment_id="her",
            adapter_version="1",
            data_origin="synthetic",
            execution_mode="replay",
            state="active",
            declaration={},
            declaration_hash="0" * 64,
            event_stream_contract_version=0,
            event_stream_last_position=0,
            created_at=func.now(),
            updated_at=func.now(),
        )
    )
    connection.execute(
        work_items.insert().values(
            work_item_id=legacy_work_item_id,
            campaign_id=legacy_campaign_id,
            candidate_id="cand:legacy-recovery",
            candidate=_candidate_payload(),
            state="queued",
            created_at=func.now(),
            updated_at=func.now(),
        )
    )
    connection.execute(
        jobs.insert().values(
            job_id=legacy_job_id,
            work_item_id=legacy_work_item_id,
            state="leased",
            available_at=func.now(),
            lease_owner="legacy-worker",
            lease_token=uuid.uuid4(),
            lease_expires_at=datetime(2000, 1, 1, tzinfo=UTC),
            attempt_count=1,
            max_attempts=3,
            command_version="1",
            idempotency_key=f"legacy-recovery:{uuid.uuid4().hex}",
            created_at=func.now(),
            updated_at=func.now(),
        )
    )
    complete_job_id = _enqueue(connection, work_item)
    lease = claim(connection, owner="complete-worker")
    assert lease is not None
    assert lease.job_id == complete_job_id
    expire_lease_now(connection, complete_job_id)

    assert len(recover_expired_leases(connection)) == ONE_JOB
    states = dict(
        connection.execute(
            select(jobs.c.job_id, jobs.c.state).where(
                jobs.c.job_id.in_((legacy_job_id, complete_job_id))
            )
        ).all()
    )
    assert states == {legacy_job_id: "leased", complete_job_id: "available"}

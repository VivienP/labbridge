"""Durable jobs: atomic claim, leases, heartbeats, retry scheduling, and lease recovery.

`docs/SPEC.md` §6 and ADR-002: one worker process over PostgreSQL-backed jobs, no broker.
Delivery is **at least once**; the guarantee against duplicate *effects* lives in the constraints
(`AI_CONTRACT.md` invariant 5, PO-02), not here. Nothing in this module may be described as
exactly-once.

Three decisions carry the correctness of the whole thing:

* **`FOR UPDATE SKIP LOCKED`** makes the claim atomic. Two workers running the same statement select
  different rows; a `SELECT` then `UPDATE` without it hands the same job to both under concurrency,
  which is the classic defect this pattern exists to remove.
* **Time comes from the database**, never from a worker. `now()` is evaluated server-side for every
  lease expiry and comparison. If workers dated their own leases, two hosts with a few seconds of
  clock skew would disagree about whether a lease is live — and the disagreement would be invisible
  until it lost a result.
* **A lease token is required to complete.** Expiry alone is not enough: a worker paused past its
  expiry wakes up believing it still owns the job, and by then another worker may have taken it. The
  token written by the claim must be presented on every heartbeat and completion, so the stale owner
  is refused rather than allowed to overwrite the new owner's work (F-005, F-006).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import ClassVar, Final

from sqlalchemy import (
    ColumnElement,
    Connection,
    and_,
    func,
    or_,
    select,
    update,
)

from labbridge.domain.lifecycle import JobState, check_job_transition
from labbridge.infrastructure.persistence.tables import campaigns, jobs, work_items
from labbridge.runtime.events import AppendedEvent, append_event, current_sequence

#: How long a claim holds a job before the lease is reclaimable. `heartbeat` extends it — but the
#: worker does not call `heartbeat` yet, so for the shipped runtime this is a hard cap on how long
#: an adapter call may take before another worker may claim the same job. Stated rather than
#: implied: the intent is a liveness floor, the delivered behaviour is a timeout.
DEFAULT_LEASE_SECONDS: Final = 60
#: Exponential, capped. A retry storm against a failing dependency is itself a failure mode.
RETRY_BASE_SECONDS: Final = 2
MAX_RETRY_SECONDS: Final = 300


class JobError(Exception):
    code: ClassVar[str] = "job_error"


class LeaseLostError(JobError):
    """The token presented is not the token that holds this job.

    Raised rather than ignored: a worker whose lease was reclaimed must not silently continue, and
    must not treat its own work as accepted.
    """

    code: ClassVar[str] = "lease_lost"

    def __init__(self, job_id: uuid.UUID) -> None:
        self.job_id = job_id
        super().__init__(
            f"job {job_id} is no longer held by the presented lease token; another worker may "
            "have claimed it after the lease expired"
        )


@dataclass(frozen=True)
class Lease:
    """Proof that this worker holds this job, and until when by the database's clock."""

    job_id: uuid.UUID
    work_item_id: uuid.UUID
    lease_token: uuid.UUID
    lease_expires_at: datetime
    attempt_count: int
    max_attempts: int
    idempotency_key: str
    correlation_id: uuid.UUID
    last_event_id: uuid.UUID

    @property
    def attempts_exhausted(self) -> bool:
        return self.attempt_count >= self.max_attempts


def enqueue(
    connection: Connection,
    *,
    campaign_id: uuid.UUID,
    work_item_id: uuid.UUID,
    idempotency_key: str,
    command_version: str,
    correlation_id: uuid.UUID,
    causation_id: uuid.UUID,
    max_attempts: int = 3,
) -> uuid.UUID | None:
    """Create a durable job, or return None when this idempotency key already has one.

    The uniqueness is a database constraint, so a duplicate submission racing itself loses in the
    index rather than in application logic (F-001, PO-02).
    """
    existing = connection.execute(
        select(jobs.c.job_id).where(jobs.c.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing is not None:
        return None

    job_id = uuid.uuid4()
    connection.execute(
        jobs.insert().values(
            job_id=job_id,
            work_item_id=work_item_id,
            state="available",
            available_at=func.now(),
            attempt_count=0,
            max_attempts=max_attempts,
            command_version=command_version,
            idempotency_key=idempotency_key,
            event_correlation_id=correlation_id,
            created_at=func.now(),
            updated_at=func.now(),
        )
    )
    appended = _append_job_event(
        connection,
        job_id,
        campaign_id=campaign_id,
        event_type="job.enqueued",
        causation_id=causation_id,
    )
    connection.execute(
        update(jobs).where(jobs.c.job_id == job_id).values(last_event_id=appended.event_id)
    )
    return job_id


def _job_event_payload(connection: Connection, job_id: uuid.UUID) -> dict[str, object]:
    row = connection.execute(select(jobs).where(jobs.c.job_id == job_id)).mappings().one()
    return {
        "work_item_id": row["work_item_id"],
        "state": row["state"],
        "available_at": row["available_at"],
        "lease_owner": row["lease_owner"],
        "lease_token": row["lease_token"],
        "lease_expires_at": row["lease_expires_at"],
        "heartbeat_at": row["heartbeat_at"],
        "attempt_count": row["attempt_count"],
        "max_attempts": row["max_attempts"],
        "command_version": row["command_version"],
        "idempotency_key": row["idempotency_key"],
        "last_failure": row["last_failure"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _campaign_id_for_job(connection: Connection, job_id: uuid.UUID) -> uuid.UUID:
    campaign_id = connection.execute(
        select(work_items.c.campaign_id)
        .select_from(jobs.join(work_items))
        .where(jobs.c.job_id == job_id)
    ).scalar_one()
    return uuid.UUID(str(campaign_id))


def _append_job_event(
    connection: Connection,
    job_id: uuid.UUID,
    *,
    event_type: str,
    campaign_id: uuid.UUID | None = None,
    causation_id: uuid.UUID | None = None,
) -> AppendedEvent:
    campaign_id = campaign_id or _campaign_id_for_job(connection, job_id)
    row = connection.execute(
        select(jobs.c.event_correlation_id, jobs.c.last_event_id).where(jobs.c.job_id == job_id)
    ).one()
    correlation_id: uuid.UUID = row.event_correlation_id
    cause = causation_id or row.last_event_id
    appended = append_event(
        connection,
        campaign_id=campaign_id,
        aggregate_id=job_id,
        aggregate_type="job",
        event_type=event_type,
        payload=_job_event_payload(connection, job_id),
        expected_version=current_sequence(
            connection,
            campaign_id=campaign_id,
            aggregate_type="job",
            aggregate_id=job_id,
        ),
        correlation_id=correlation_id,
        causation_id=cause,
    )
    connection.execute(
        update(jobs).where(jobs.c.job_id == job_id).values(last_event_id=appended.event_id)
    )
    return appended


def event_context(connection: Connection, job_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Return the durable correlation and latest causal event for a job."""
    row = connection.execute(
        select(jobs.c.event_correlation_id, jobs.c.last_event_id).where(jobs.c.job_id == job_id)
    ).one()
    if row.event_correlation_id is None or row.last_event_id is None:
        raise RuntimeError(f"job {job_id} has no complete event context")
    return row.event_correlation_id, row.last_event_id


def claim(
    connection: Connection, *, owner: str, lease_seconds: int = DEFAULT_LEASE_SECONDS
) -> Lease | None:
    """Atomically take one available job, or return None when there is nothing to do.

    `SKIP LOCKED` is what makes this safe to run from many workers at once: a row already locked by
    a concurrent claim is passed over rather than waited on, so claims neither block nor collide.
    """
    token = uuid.uuid4()
    claimable = connection.execute(
        select(jobs.c.job_id)
        .select_from(jobs.join(work_items).join(campaigns))
        .where(
            and_(
                jobs.c.state == "available",
                jobs.c.available_at <= func.now(),
                campaigns.c.event_stream_contract_version == 1,
            )
        )
        .order_by(jobs.c.available_at)
        .limit(1)
        .with_for_update(skip_locked=True, of=jobs)
    ).scalar_one_or_none()
    if claimable is None:
        return None
    row = connection.execute(
        update(jobs)
        .where(jobs.c.job_id == claimable, jobs.c.state == "available")
        .values(
            state="leased",
            lease_owner=owner,
            lease_token=token,
            lease_expires_at=func.now() + _interval(lease_seconds),
            heartbeat_at=func.now(),
            attempt_count=jobs.c.attempt_count + 1,
            updated_at=func.now(),
        )
        .returning(
            jobs.c.job_id,
            jobs.c.work_item_id,
            jobs.c.lease_expires_at,
            jobs.c.attempt_count,
            jobs.c.max_attempts,
            jobs.c.idempotency_key,
            jobs.c.event_correlation_id,
            jobs.c.last_event_id,
        )
    ).one_or_none()
    if row is None:
        return None
    appended = _append_job_event(connection, row.job_id, event_type="job.leased")
    return Lease(
        job_id=row.job_id,
        work_item_id=row.work_item_id,
        lease_token=token,
        lease_expires_at=row.lease_expires_at,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        idempotency_key=row.idempotency_key,
        correlation_id=row.event_correlation_id,
        last_event_id=appended.event_id,
    )


def _interval(seconds: int) -> ColumnElement[timedelta]:
    """A server-side interval, so the database evaluates every deadline against its own clock."""
    return func.make_interval(0, 0, 0, 0, 0, 0, seconds)


def _held(lease: Lease) -> ColumnElement[bool]:
    """The predicate that says this lease still owns this job, evaluated by the database."""
    return and_(
        jobs.c.job_id == lease.job_id,
        jobs.c.lease_token == lease.lease_token,
        jobs.c.lease_expires_at > func.now(),
    )


def heartbeat(
    connection: Connection, lease: Lease, *, lease_seconds: int = DEFAULT_LEASE_SECONDS
) -> datetime:
    """Extend the lease. Raises when the lease is no longer held, which is a real outcome."""
    row = connection.execute(
        update(jobs)
        .where(_held(lease))
        .values(
            heartbeat_at=func.now(),
            lease_expires_at=func.now() + _interval(lease_seconds),
            updated_at=func.now(),
        )
        .returning(jobs.c.lease_expires_at)
    ).one_or_none()
    if row is None:
        raise LeaseLostError(lease.job_id)
    _append_job_event(connection, lease.job_id, event_type="job.heartbeat")
    expires: datetime = row.lease_expires_at
    return expires


def mark_running(connection: Connection, lease: Lease) -> None:
    check_job_transition("leased", "running")
    _transition(connection, lease, "running", event_type="job.started")


def _transition(
    connection: Connection,
    lease: Lease,
    state: JobState,
    *,
    event_type: str,
    causation_id: uuid.UUID | None = None,
    **values: object,
) -> None:
    result = connection.execute(
        update(jobs).where(_held(lease)).values(state=state, updated_at=func.now(), **values)
    )
    if result.rowcount != 1:
        raise LeaseLostError(lease.job_id)
    _append_job_event(connection, lease.job_id, event_type=event_type, causation_id=causation_id)


def complete(
    connection: Connection, lease: Lease, *, causation_id: uuid.UUID | None = None
) -> None:
    """Finish successfully, releasing the lease.

    Called inside the same transaction that records the outcome: if that transaction rolls back the
    job stays leased and is retried, rather than being marked done for work that was not committed
    (F-025).
    """
    _transition(
        connection,
        lease,
        "succeeded",
        event_type="job.succeeded",
        causation_id=causation_id,
        lease_owner=None,
        lease_token=None,
        lease_expires_at=None,
    )


def schedule_retry(
    connection: Connection,
    lease: Lease,
    *,
    failure: dict[str, object] | None = None,
    causation_id: uuid.UUID | None = None,
) -> datetime | None:
    """Return the job for another attempt, or fail it terminally when attempts are exhausted.

    Backoff is exponential in the attempt count and capped, and the delay is applied by the database
    so a worker's clock cannot shorten it.
    """
    if lease.attempts_exhausted:
        _transition(
            connection,
            lease,
            "failed_terminal",
            event_type="job.failed_terminal",
            causation_id=causation_id,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            last_failure=failure,
        )
        return None

    delay = min(RETRY_BASE_SECONDS**lease.attempt_count, MAX_RETRY_SECONDS)
    row = connection.execute(
        update(jobs)
        .where(_held(lease))
        .values(
            state="available",
            available_at=func.now() + _interval(delay),
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            last_failure=failure,
            updated_at=func.now(),
        )
        .returning(jobs.c.available_at)
    ).one_or_none()
    if row is None:
        raise LeaseLostError(lease.job_id)
    _append_job_event(
        connection, lease.job_id, event_type="job.available", causation_id=causation_id
    )
    available_at: datetime = row.available_at
    return available_at


def fail_terminally(
    connection: Connection,
    lease: Lease,
    *,
    failure: dict[str, object] | None = None,
    causation_id: uuid.UUID | None = None,
) -> None:
    """No retry: the failure is not retryable whatever the attempt count says."""
    _transition(
        connection,
        lease,
        "failed_terminal",
        event_type="job.failed_terminal",
        causation_id=causation_id,
        lease_owner=None,
        lease_token=None,
        lease_expires_at=None,
        last_failure=failure,
    )


def recover_expired_leases(connection: Connection) -> int:
    """Return every job whose lease has expired to the queue, and report how many.

    This is what *would* make a killed worker recoverable — but **nothing calls it in production**.
    There is no sweeper and no worker loop, so today a killed worker strands its job until an
    operator runs recovery by hand. The mechanism is here and tested; the schedule is not (F-005).

    A job that has already used its attempts is failed terminally instead of looping forever.
    """
    expired = and_(
        or_(jobs.c.state == "leased", jobs.c.state == "running"),
        jobs.c.lease_expires_at <= func.now(),
    )
    recoverable = connection.execute(
        select(jobs.c.job_id, jobs.c.attempt_count, jobs.c.max_attempts)
        .select_from(jobs.join(work_items).join(campaigns))
        .where(expired, campaigns.c.event_stream_contract_version == 1)
        .with_for_update(skip_locked=True, of=jobs)
    ).all()
    for row in recoverable:
        exhausted = row.attempt_count >= row.max_attempts
        values: dict[str, object] = {
            "state": "failed_terminal" if exhausted else "available",
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at": None,
            "updated_at": func.now(),
        }
        if exhausted:
            values["last_failure"] = {"failure_code": "lease_expired"}
        else:
            values["available_at"] = func.now()
        connection.execute(update(jobs).where(jobs.c.job_id == row.job_id).values(**values))
        _append_job_event(
            connection,
            row.job_id,
            event_type="job.failed_terminal" if exhausted else "job.available",
        )
    return len(recoverable)


def expire_lease_now(connection: Connection, job_id: uuid.UUID) -> None:
    """Force a lease to look expired. For tests and for an operator reclaiming a stuck job.

    Kept here rather than in the test suite so the statement that fakes expiry is the same shape as
    the one that observes it, and so an operator has a supported way to do it.
    """
    connection.execute(
        update(jobs)
        .where(jobs.c.job_id == job_id)
        .values(lease_expires_at=func.now() - _interval(1), updated_at=func.now())
    )
    _append_job_event(connection, job_id, event_type="job.lease_expired")

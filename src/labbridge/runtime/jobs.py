"""Durable jobs: atomic claim, leases, heartbeats, retry scheduling, and lease recovery.

`docs/SPEC.md` §6 and ADR-002: one worker process over PostgreSQL-backed jobs, no broker.
Delivery is **at least once**; the guarantee against duplicate *effects* lives in the constraints
(`AI_CONTRACT.md` invariant 5, PO-02), not here. Nothing in this module may be described as
exactly-once.

Four decisions carry the correctness of the whole thing:

* **`INSERT ... ON CONFLICT DO NOTHING` on the instruction key** makes enqueuing idempotent. The
  unique index arbitrates, so two callers racing on one logical instruction produce one job — a
  prior `SELECT` would let both through and turn the loser into an integrity error.
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
from decimal import Decimal
from typing import ClassVar, Final, Literal

from sqlalchemy import (
    ColumnElement,
    Connection,
    and_,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from labbridge.domain.campaigns import DEFAULT_RETRY_POLICY
from labbridge.domain.idempotency import InstructionConflictError
from labbridge.domain.lifecycle import JobState, check_job_transition
from labbridge.infrastructure.persistence.tables import attempts, campaigns, jobs, work_items
from labbridge.runtime.budgets import release_outstanding_without_attempt, reserve
from labbridge.runtime.events import (
    READABLE_STREAM_CONTRACT_VERSIONS,
    AppendedEvent,
    append_event,
    current_sequence,
)

#: How long a claim holds a job before the lease is reclaimable. `heartbeat` extends it — but the
#: worker does not call `heartbeat` yet, so for the shipped runtime this is a hard cap on how long
#: an adapter call may take before another worker may claim the same job. Stated rather than
#: implied: the intent is a liveness floor, the delivered behaviour is a timeout.
DEFAULT_LEASE_SECONDS: Final = 60


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
    #: The fencing token this claim was issued under. Every reclaim increments it, so a lease held
    #: across an expiry is recognisable as stale by comparison rather than by trusting a clock.
    lease_generation: int
    lease_expires_at: datetime
    attempt_count: int
    max_attempts: int
    idempotency_key: str
    correlation_id: uuid.UUID
    last_event_id: uuid.UUID
    budget_reservation_id: uuid.UUID
    reserved_amount: Decimal
    budget_unit: str

    @property
    def attempts_exhausted(self) -> bool:
        return self.attempt_count >= self.max_attempts


@dataclass(frozen=True)
class EnqueuedJob:
    """The job an instruction resolves to, and whether this call is what created it.

    `created` is the only thing that distinguishes the two outcomes, and it is returned rather than
    inferred: a caller that needs to know whether it produced the work — to emit an event, to count
    a submission — cannot recover that from the identifier alone.
    """

    job_id: uuid.UUID
    created: bool


RetryScheduleStatus = Literal[
    "scheduled", "retry_cap_reached", "campaign_cancelled", "budget_exhausted"
]


@dataclass(frozen=True)
class RetryScheduleResult:
    status: RetryScheduleStatus
    available_at: datetime | None = None


def enqueue(
    connection: Connection,
    *,
    campaign_id: uuid.UUID,
    work_item_id: uuid.UUID,
    instruction_key: str,
    command_version: str,
    correlation_id: uuid.UUID,
    causation_id: uuid.UUID,
    max_attempts: int = 3,
) -> EnqueuedJob:
    """Create the durable job for one logical instruction, or return the one that already exists.

    The instruction key is the identity of the work, not of the request that asked for it
    (`domain.idempotency.work_item_instruction_key`). Enqueuing the same instruction again is a
    no-op whoever asks and however often.

    **The unique index decides, not a prior read.** `ON CONFLICT DO NOTHING` waits for a concurrent
    inserter to commit or abort and then reports what actually happened, so two callers racing on
    one instruction produce one job and one `job.enqueued` event rather than an integrity error
    escaping into the caller's transaction (F-001, PO-02, ADR-015).
    """
    job_id = uuid.uuid4()
    inserted = connection.execute(
        pg_insert(jobs)
        .values(
            job_id=job_id,
            work_item_id=work_item_id,
            state="available",
            available_at=func.now(),
            attempt_count=0,
            max_attempts=max_attempts,
            command_version=command_version,
            idempotency_key=instruction_key,
            event_correlation_id=correlation_id,
            created_at=func.now(),
            updated_at=func.now(),
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(jobs.c.job_id)
    ).one_or_none()
    if inserted is None:
        # The conflicting insert has settled by the time `ON CONFLICT DO NOTHING` returns nothing,
        # so this read cannot miss it: had that transaction rolled back, the insert above would
        # have proceeded instead. This depends on READ COMMITTED, where each statement takes a
        # fresh snapshot — under a stricter isolation level PostgreSQL raises a serialisation
        # failure here rather than returning a stale answer, which is loud rather than wrong.
        existing = connection.execute(
            select(jobs.c.job_id, jobs.c.work_item_id, jobs.c.command_version).where(
                jobs.c.idempotency_key == instruction_key
            )
        ).one()
        # Both halves of the identity are re-checked, not just the work item: the key is built from
        # the work item *and* the command version, so a hand-built key that agrees on one and not
        # the other would otherwise return a job for a command the caller did not ask for.
        if existing.work_item_id != work_item_id or existing.command_version != command_version:
            raise InstructionConflictError(
                instruction_key, stored=existing.work_item_id, offered=work_item_id
            )
        return EnqueuedJob(job_id=existing.job_id, created=False)

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
    return EnqueuedJob(job_id=job_id, created=True)


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
        "lease_generation": row["lease_generation"],
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
    candidate_campaign = connection.execute(
        select(work_items.c.campaign_id)
        .select_from(jobs.join(work_items).join(campaigns))
        .where(
            and_(
                jobs.c.state == "available",
                jobs.c.available_at <= func.now(),
                campaigns.c.event_stream_contract_version.in_(READABLE_STREAM_CONTRACT_VERSIONS),
                campaigns.c.state == "active",
            )
        )
        .order_by(jobs.c.available_at)
        .limit(1)
    ).scalar_one_or_none()
    if candidate_campaign is None:
        return None
    locked_campaign = connection.execute(
        select(campaigns.c.campaign_id)
        .where(
            campaigns.c.campaign_id == candidate_campaign,
            campaigns.c.state == "active",
            campaigns.c.event_stream_contract_version.in_(READABLE_STREAM_CONTRACT_VERSIONS),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if locked_campaign is None:
        return None
    claimable = connection.execute(
        select(
            jobs.c.job_id,
            jobs.c.work_item_id,
            jobs.c.lease_generation,
        )
        .select_from(jobs.join(work_items))
        .where(
            work_items.c.campaign_id == locked_campaign,
            jobs.c.state == "available",
            jobs.c.available_at <= func.now(),
        )
        .order_by(jobs.c.available_at)
        .limit(1)
        .with_for_update(skip_locked=True, of=jobs)
    ).one_or_none()
    if claimable is None:
        return None
    reservation = reserve(
        connection,
        campaign_id=locked_campaign,
        work_item_id=claimable.work_item_id,
        job_id=claimable.job_id,
        lease_generation=int(claimable.lease_generation) + 1,
    )
    if reservation is None:
        return None
    row = connection.execute(
        update(jobs)
        .where(jobs.c.job_id == claimable.job_id, jobs.c.state == "available")
        .values(
            state="leased",
            lease_owner=owner,
            lease_token=token,
            # Incremented, never reset: the generation is what makes a stale holder detectable
            # after its lease was reclaimed, including when the reclaimer handed the job back to
            # the same owner name.
            lease_generation=jobs.c.lease_generation + 1,
            lease_expires_at=func.now() + _interval(lease_seconds),
            heartbeat_at=func.now(),
            attempt_count=jobs.c.attempt_count + 1,
            updated_at=func.now(),
        )
        .returning(
            jobs.c.job_id,
            jobs.c.work_item_id,
            jobs.c.lease_generation,
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
        lease_generation=int(row.lease_generation),
        lease_expires_at=row.lease_expires_at,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        idempotency_key=row.idempotency_key,
        correlation_id=row.event_correlation_id,
        last_event_id=appended.event_id,
        budget_reservation_id=reservation.entry_id,
        reserved_amount=reservation.amount,
        budget_unit=reservation.unit,
    )


def _interval(seconds: int) -> ColumnElement[timedelta]:
    """A server-side interval, so the database evaluates every deadline against its own clock."""
    return func.make_interval(0, 0, 0, 0, 0, 0, seconds)


def _held(lease: Lease) -> ColumnElement[bool]:
    """The predicate that says this lease still owns this job, evaluated by the database.

    Three conditions, and each rules out a different way of being wrong: the generation rules out a
    holder whose lease was reclaimed and reissued, the token rules out one that guessed the
    generation, and the expiry rules out one whose lease simply ran out. The clock is the
    database's, never the worker's.
    """
    return and_(
        jobs.c.job_id == lease.job_id,
        jobs.c.lease_token == lease.lease_token,
        jobs.c.lease_generation == lease.lease_generation,
        jobs.c.lease_expires_at > func.now(),
    )


def assert_held(connection: Connection, lease: Lease) -> None:
    """Raise unless this lease still owns this job, right now, on this connection.

    Called as the first statement of any transaction that is about to write an accepted effect. A
    check performed *before* opening that transaction proves nothing: the lease can expire and be
    reclaimed in the gap, and the writer would then commit on behalf of a job it no longer holds.
    Inside the transaction the row is locked, so the answer cannot change before the commit that
    depends on it.

    `FOR UPDATE` rather than a plain read for exactly that reason — it holds the row against a
    concurrent reclaim until this transaction ends, one way or the other.
    """
    held = connection.execute(
        select(jobs.c.job_id).where(_held(lease)).with_for_update()
    ).one_or_none()
    if held is None:
        raise LeaseLostError(lease.job_id)


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
) -> RetryScheduleResult:
    """Return the job for another attempt, or fail it terminally when attempts are exhausted.

    Backoff is exponential in the attempt count and capped, and the delay is applied by the database
    so a worker's clock cannot shorten it.
    """
    campaign_state = connection.execute(
        select(campaigns.c.state)
        .select_from(
            campaigns.join(work_items, work_items.c.campaign_id == campaigns.c.campaign_id)
        )
        .where(work_items.c.work_item_id == lease.work_item_id)
        .with_for_update(of=campaigns)
    ).scalar_one()
    if campaign_state in ("cancelled", "budget_exhausted"):
        _transition(
            connection,
            lease,
            "cancelled",
            event_type="job.cancelled",
            causation_id=causation_id,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            last_failure=failure,
        )
        return RetryScheduleResult(
            status=("campaign_cancelled" if campaign_state == "cancelled" else "budget_exhausted")
        )
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
        return RetryScheduleResult(status="retry_cap_reached")

    delay = DEFAULT_RETRY_POLICY.backoff_seconds(lease.attempt_count)
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
    return RetryScheduleResult(status="scheduled", available_at=available_at)


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


def cancel_available_for_campaign(
    connection: Connection,
    campaign_id: uuid.UUID,
    *,
    causation_id: uuid.UUID,
    reason: str,
) -> tuple[uuid.UUID, ...]:
    """Cancel only unleased work; an existing lease retains its right to finish."""
    rows = connection.execute(
        select(jobs.c.job_id, jobs.c.work_item_id)
        .select_from(jobs.join(work_items))
        .where(
            work_items.c.campaign_id == campaign_id,
            jobs.c.state == "available",
        )
        .with_for_update(of=jobs)
    ).all()
    cancelled_items: list[uuid.UUID] = []
    for row in rows:
        connection.execute(
            update(jobs)
            .where(jobs.c.job_id == row.job_id, jobs.c.state == "available")
            .values(
                state="cancelled",
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                updated_at=func.now(),
            )
        )
        appended = _append_job_event(
            connection,
            row.job_id,
            event_type="job.cancelled",
            causation_id=causation_id,
        )
        live_job = connection.execute(
            select(jobs.c.job_id)
            .where(
                jobs.c.work_item_id == row.work_item_id,
                jobs.c.state.in_(("leased", "running")),
            )
            .limit(1)
        ).scalar_one_or_none()
        if live_job is not None:
            continue
        changed = connection.execute(
            update(work_items)
            .where(
                work_items.c.work_item_id == row.work_item_id,
                work_items.c.state.in_(("queued", "quarantined")),
            )
            .values(
                state="cancelled",
                quarantine_reason=None,
                updated_at=func.now(),
            )
        )
        if changed.rowcount != 1:
            continue
        event_row = connection.execute(
            select(jobs.c.event_correlation_id).where(jobs.c.job_id == row.job_id)
        ).one()
        append_event(
            connection,
            campaign_id=campaign_id,
            aggregate_id=row.work_item_id,
            aggregate_type="work_item",
            event_type="work_item.cancelled",
            payload={"state": "cancelled", "reason": reason},
            expected_version=current_sequence(
                connection,
                campaign_id=campaign_id,
                aggregate_type="work_item",
                aggregate_id=row.work_item_id,
            ),
            correlation_id=event_row.event_correlation_id,
            causation_id=appended.event_id,
        )
        cancelled_items.append(row.work_item_id)
    return tuple(cancelled_items)


@dataclass(frozen=True)
class ReclaimedLease:
    """One job taken back from a holder that stopped proving it was alive."""

    job_id: uuid.UUID
    work_item_id: uuid.UUID
    #: The generation the stale holder was using. Recorded so the reclaim is auditable: it says
    #: which lease was fenced out, not merely that one was.
    fenced_generation: int
    lease_generation: int
    previous_owner: str | None
    exhausted: bool


def recover_expired_leases(connection: Connection) -> list[ReclaimedLease]:
    """Return every job whose lease has expired to the queue, and describe each reclaim.

    Called by `runtime.reconciliation.reconcile`, which runs once at worker startup and behind
    `labbridge reconcile`. There is deliberately no sweeper process: a reclaim is cheap and belongs
    where a worker is about to look for work anyway, and a second daemon would be another thing to
    supervise (F-003, F-005).

    **The generation is incremented here, not only on the next claim.** Bumping it at reclaim time
    is what fences the stale holder out immediately: from this commit onwards its `_held` predicate
    cannot match, so it can neither heartbeat nor finalise, whether or not anyone claims the job
    next. Waiting for the next claim would leave a window in which the old holder still looked
    current.

    A job that has already used its attempts is failed terminally instead of looping forever.
    """
    expired = and_(
        or_(jobs.c.state == "leased", jobs.c.state == "running"),
        jobs.c.lease_expires_at <= func.now(),
    )
    recoverable = connection.execute(
        select(
            jobs.c.job_id,
            jobs.c.work_item_id,
            jobs.c.lease_owner,
            jobs.c.lease_generation,
            jobs.c.attempt_count,
            jobs.c.max_attempts,
        )
        .select_from(jobs.join(work_items).join(campaigns))
        .where(
            expired,
            campaigns.c.event_stream_contract_version.in_(READABLE_STREAM_CONTRACT_VERSIONS),
        )
        .with_for_update(skip_locked=True, of=jobs)
    ).all()
    reclaimed: list[ReclaimedLease] = []
    for row in recoverable:
        running_attempt = connection.execute(
            select(attempts.c.attempt_id).where(
                attempts.c.job_id == row.job_id,
                attempts.c.state == "running",
            )
        ).scalar_one_or_none()
        if running_attempt is None:
            release_outstanding_without_attempt(connection, job_id=row.job_id)
        exhausted = row.attempt_count >= row.max_attempts
        values: dict[str, object] = {
            "state": "failed_terminal" if exhausted else "available",
            "lease_owner": None,
            "lease_token": None,
            "lease_generation": jobs.c.lease_generation + 1,
            "lease_expires_at": None,
            "updated_at": func.now(),
        }
        if exhausted:
            values["last_failure"] = {"failure_code": "lease_expired"}
        else:
            values["available_at"] = func.now()
        bumped = connection.execute(
            update(jobs)
            .where(jobs.c.job_id == row.job_id)
            .values(**values)
            .returning(jobs.c.lease_generation)
        ).one()
        job_event = _append_job_event(
            connection,
            row.job_id,
            event_type="job.failed_terminal" if exhausted else "job.available",
        )
        if exhausted:
            reason = "retry_cap_reached:lease_lost"
            changed = connection.execute(
                update(work_items)
                .where(
                    work_items.c.work_item_id == row.work_item_id,
                    work_items.c.state.in_(("queued", "quarantined")),
                )
                .values(
                    state="quarantined",
                    quarantine_reason=reason,
                    updated_at=func.now(),
                )
            )
            if changed.rowcount == 1:
                context = connection.execute(
                    select(jobs.c.event_correlation_id).where(jobs.c.job_id == row.job_id)
                ).one()
                append_event(
                    connection,
                    campaign_id=_campaign_id_for_job(connection, row.job_id),
                    aggregate_id=row.work_item_id,
                    aggregate_type="work_item",
                    event_type="work_item.quarantined",
                    payload={"state": "quarantined", "reason": reason},
                    expected_version=current_sequence(
                        connection,
                        campaign_id=_campaign_id_for_job(connection, row.job_id),
                        aggregate_type="work_item",
                        aggregate_id=row.work_item_id,
                    ),
                    correlation_id=context.event_correlation_id,
                    causation_id=job_event.event_id,
                )
        reclaimed.append(
            ReclaimedLease(
                job_id=row.job_id,
                work_item_id=row.work_item_id,
                fenced_generation=int(row.lease_generation),
                lease_generation=int(bumped.lease_generation),
                previous_owner=row.lease_owner,
                exhausted=exhausted,
            )
        )
    return reclaimed


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

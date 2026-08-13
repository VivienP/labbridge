"""PostgreSQL budget reservations and campaign control transactions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar

from sqlalchemy import Connection, Engine, and_, case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from labbridge.application.campaigns import (
    CampaignAction,
    CampaignControlRepository,
    CampaignControlResult,
)
from labbridge.domain.idempotency import (
    IdempotencyConflictError,
    check_request_fingerprint,
)
from labbridge.domain.lifecycle import CampaignState, check_campaign_transition
from labbridge.infrastructure.persistence.tables import (
    budget_ledger,
    campaigns,
    events,
    idempotency_keys,
    jobs,
)
from labbridge.runtime.events import (
    COMPLETE_STREAM_CONTRACT_VERSION,
    ExpectedVersionError,
    append_event,
    current_sequence,
)


class BudgetError(RuntimeError):
    code: ClassVar[str] = "budget_error"


class ReservationSettledError(BudgetError):
    code = "budget_reservation_already_settled"

    def __init__(self, reservation_id: uuid.UUID) -> None:
        self.reservation_id = reservation_id
        super().__init__(f"budget reservation {reservation_id} already has a settlement")


class CampaignNotFoundError(BudgetError):
    code = "campaign_not_found"

    def __init__(self, campaign_id: uuid.UUID) -> None:
        self.campaign_id = campaign_id
        super().__init__(f"campaign {campaign_id} does not exist")


@dataclass(frozen=True)
class Reservation:
    entry_id: uuid.UUID
    campaign_id: uuid.UUID
    work_item_id: uuid.UUID
    job_id: uuid.UUID
    lease_generation: int
    amount: Decimal
    unit: str


@dataclass(frozen=True)
class Settlement:
    entry_id: uuid.UUID
    reservation_entry_id: uuid.UUID
    kind: str
    amount: Decimal
    unit: str
    reserved_amount: Decimal
    actual_amount: Decimal | None


@dataclass(frozen=True)
class BudgetUsage:
    hard_limit: Decimal
    unit: str
    reserved: Decimal
    consumed: Decimal
    released: Decimal
    outstanding: Decimal
    remaining: Decimal


def _append_budget_event(
    connection: Connection,
    *,
    entry_id: uuid.UUID,
    campaign_id: uuid.UUID,
    work_item_id: uuid.UUID,
    job_id: uuid.UUID,
    attempt_id: uuid.UUID | None,
    lease_generation: int,
    reservation_entry_id: uuid.UUID | None,
    kind: str,
    amount: Decimal,
    unit: str,
    reason: str,
    recorded_at: object,
) -> None:
    contract_version = connection.execute(
        select(campaigns.c.event_stream_contract_version).where(
            campaigns.c.campaign_id == campaign_id
        )
    ).scalar_one()
    if contract_version != COMPLETE_STREAM_CONTRACT_VERSION:
        return
    context = connection.execute(
        select(jobs.c.event_correlation_id, jobs.c.last_event_id).where(jobs.c.job_id == job_id)
    ).one()
    if context.event_correlation_id is None or context.last_event_id is None:
        raise RuntimeError(f"job {job_id} has no complete event context")
    latest_budget_event = connection.execute(
        select(events.c.event_id)
        .where(
            events.c.campaign_id == campaign_id,
            events.c.aggregate_type == "budget",
            events.c.aggregate_id == (reservation_entry_id or entry_id),
        )
        .order_by(events.c.sequence.desc())
        .limit(1)
    ).scalar_one_or_none()
    append_event(
        connection,
        campaign_id=campaign_id,
        aggregate_id=reservation_entry_id or entry_id,
        aggregate_type="budget",
        event_type=(
            "budget.adjusted" if kind in ("adjusted_up", "adjusted_down") else f"budget.{kind}"
        ),
        payload={
            "entry_id": entry_id,
            "work_item_id": work_item_id,
            "job_id": job_id,
            "attempt_id": attempt_id,
            "lease_generation": lease_generation,
            "reservation_entry_id": reservation_entry_id,
            "kind": kind,
            "amount": amount,
            "unit": unit,
            "reason": reason,
            "recorded_at": recorded_at,
        },
        expected_version=current_sequence(
            connection,
            campaign_id=campaign_id,
            aggregate_type="budget",
            aggregate_id=reservation_entry_id or entry_id,
        ),
        correlation_id=context.event_correlation_id,
        causation_id=latest_budget_event or context.last_event_id,
    )


def budget_usage(connection: Connection, campaign_id: uuid.UUID) -> BudgetUsage:
    campaign = connection.execute(
        select(campaigns.c.hard_budget, campaigns.c.budget_unit).where(
            campaigns.c.campaign_id == campaign_id
        )
    ).one()
    rows = connection.execute(
        select(
            budget_ledger.c.entry_id,
            budget_ledger.c.reservation_entry_id,
            budget_ledger.c.kind,
            budget_ledger.c.amount,
        ).where(budget_ledger.c.campaign_id == campaign_id)
    ).all()
    settled = {row.reservation_entry_id for row in rows if row.reservation_entry_id is not None}
    reserved = sum((Decimal(row.amount) for row in rows if row.kind == "reserved"), Decimal(0))
    consumed = sum(
        (
            Decimal(row.amount) if row.kind in ("consumed", "adjusted_up") else -Decimal(row.amount)
            for row in rows
            if row.kind in ("consumed", "adjusted_up", "adjusted_down")
        ),
        Decimal(0),
    )
    released = sum((Decimal(row.amount) for row in rows if row.kind == "released"), Decimal(0))
    outstanding = sum(
        (
            Decimal(row.amount)
            for row in rows
            if row.kind == "reserved" and row.entry_id not in settled
        ),
        Decimal(0),
    )
    hard_limit = Decimal(campaign.hard_budget)
    return BudgetUsage(
        hard_limit=hard_limit,
        unit=campaign.budget_unit,
        reserved=reserved,
        consumed=consumed,
        released=released,
        outstanding=outstanding,
        remaining=max(hard_limit - consumed - outstanding, Decimal(0)),
    )


def _latest_campaign_event(connection: Connection, campaign_id: uuid.UUID) -> uuid.UUID:
    event_id = connection.execute(
        select(events.c.event_id)
        .where(
            events.c.campaign_id == campaign_id,
            events.c.aggregate_type == "campaign",
            events.c.aggregate_id == campaign_id,
        )
        .order_by(events.c.sequence.desc())
        .limit(1)
    ).scalar_one()
    return uuid.UUID(str(event_id))


def _campaign_control_event(
    connection: Connection,
    campaign_id: uuid.UUID,
    *,
    event_name: str,
    state: CampaignState,
    expected_version: int,
    idempotency_key: str | None,
    reason: str | None = None,
) -> int:
    root = connection.execute(
        select(events.c.correlation_id).where(
            events.c.campaign_id == campaign_id,
            events.c.event_type == "campaign.created",
        )
    ).scalar_one()
    appended = append_event(
        connection,
        campaign_id=campaign_id,
        aggregate_id=campaign_id,
        aggregate_type="campaign",
        event_type=f"campaign.{event_name}",
        payload={"state": state, "reason": reason},
        expected_version=expected_version,
        correlation_id=root,
        causation_id=_latest_campaign_event(connection, campaign_id),
        idempotency_key=idempotency_key,
    )
    return appended.sequence


def exhaust_campaign(
    connection: Connection, campaign_id: uuid.UUID, *, reason: str | None = None
) -> None:
    """Enter the terminal budget state and cancel work that has not been leased."""
    row = connection.execute(
        select(campaigns.c.state).where(campaigns.c.campaign_id == campaign_id).with_for_update()
    ).one_or_none()
    if row is None:
        raise CampaignNotFoundError(campaign_id)
    if row.state == "budget_exhausted":
        return
    if row.state != "active":
        return
    version = current_sequence(
        connection,
        campaign_id=campaign_id,
        aggregate_type="campaign",
        aggregate_id=campaign_id,
    )
    connection.execute(
        update(campaigns)
        .where(campaigns.c.campaign_id == campaign_id, campaigns.c.state == "active")
        .values(state="budget_exhausted", updated_at=func.now())
    )
    _campaign_control_event(
        connection,
        campaign_id,
        event_name="budget_exhausted",
        state="budget_exhausted",
        expected_version=version,
        idempotency_key=None,
        reason=reason or "the next declared attempt estimate would exceed the hard budget",
    )
    from labbridge.runtime.jobs import cancel_available_for_campaign  # noqa: PLC0415

    cancel_available_for_campaign(
        connection,
        campaign_id,
        causation_id=_latest_campaign_event(connection, campaign_id),
        reason="campaign budget exhausted",
    )


def reserve(
    connection: Connection,
    *,
    campaign_id: uuid.UUID,
    work_item_id: uuid.UUID,
    job_id: uuid.UUID,
    lease_generation: int,
) -> Reservation | None:
    """Reserve one estimate while holding the campaign row lock."""
    campaign = connection.execute(
        select(
            campaigns.c.state,
            campaigns.c.hard_budget,
            campaigns.c.per_attempt_estimate,
            campaigns.c.budget_unit,
        )
        .where(campaigns.c.campaign_id == campaign_id)
        .with_for_update()
    ).one_or_none()
    if campaign is None:
        raise CampaignNotFoundError(campaign_id)
    if campaign.state != "active":
        return None
    settled_reservations = select(budget_ledger.c.reservation_entry_id).where(
        budget_ledger.c.reservation_entry_id.is_not(None)
    )
    committed = connection.execute(
        select(
            func.coalesce(
                func.sum(
                    case(
                        (budget_ledger.c.kind == "consumed", budget_ledger.c.amount),
                        (budget_ledger.c.kind == "adjusted_up", budget_ledger.c.amount),
                        (budget_ledger.c.kind == "adjusted_down", -budget_ledger.c.amount),
                        (
                            and_(
                                budget_ledger.c.kind == "reserved",
                                budget_ledger.c.entry_id.not_in(settled_reservations),
                            ),
                            budget_ledger.c.amount,
                        ),
                        else_=0,
                    )
                ),
                0,
            )
        ).where(budget_ledger.c.campaign_id == campaign_id)
    ).scalar_one()
    hard_budget = Decimal(campaign.hard_budget)
    estimate = Decimal(campaign.per_attempt_estimate)
    if Decimal(committed) + estimate > hard_budget:
        exhaust_campaign(connection, campaign_id)
        return None
    entry_id = uuid.uuid4()
    reservation_row = connection.execute(
        budget_ledger.insert()
        .values(
            entry_id=entry_id,
            campaign_id=campaign_id,
            work_item_id=work_item_id,
            job_id=job_id,
            attempt_id=None,
            lease_generation=lease_generation,
            reservation_entry_id=None,
            kind="reserved",
            amount=estimate,
            unit=campaign.budget_unit,
            reason="declared per-attempt estimate reserved before adapter execution",
            recorded_at=func.now(),
        )
        .returning(budget_ledger.c.recorded_at)
    ).one()
    _append_budget_event(
        connection,
        entry_id=entry_id,
        campaign_id=campaign_id,
        work_item_id=work_item_id,
        job_id=job_id,
        attempt_id=None,
        lease_generation=lease_generation,
        reservation_entry_id=None,
        kind="reserved",
        amount=estimate,
        unit=campaign.budget_unit,
        reason="declared per-attempt estimate reserved before adapter execution",
        recorded_at=reservation_row.recorded_at,
    )
    return Reservation(
        entry_id=entry_id,
        campaign_id=campaign_id,
        work_item_id=work_item_id,
        job_id=job_id,
        lease_generation=lease_generation,
        amount=estimate,
        unit=campaign.budget_unit,
    )


def _settle(
    connection: Connection,
    reservation_id: uuid.UUID,
    *,
    kind: str,
    reason: str,
    attempt_id: uuid.UUID | None,
    actual_amount: Decimal | None = None,
) -> Settlement:
    reserved = (
        connection.execute(
            select(budget_ledger)
            .where(
                budget_ledger.c.entry_id == reservation_id,
                budget_ledger.c.kind == "reserved",
            )
            .with_for_update()
        )
        .mappings()
        .one()
    )
    existing = (
        connection.execute(
            select(budget_ledger).where(
                budget_ledger.c.reservation_entry_id == reservation_id,
                budget_ledger.c.kind.in_(("consumed", "released")),
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if kind == "consumed" and existing["kind"] == kind and existing["attempt_id"] == attempt_id:
            recorded_actual = Decimal(existing["amount"])
            adjustment = (
                connection.execute(
                    select(budget_ledger).where(
                        budget_ledger.c.reservation_entry_id == reservation_id,
                        budget_ledger.c.kind.in_(("adjusted_up", "adjusted_down")),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if adjustment is not None:
                recorded_actual += (
                    Decimal(adjustment["amount"])
                    if adjustment["kind"] == "adjusted_up"
                    else -Decimal(adjustment["amount"])
                )
            requested_actual = actual_amount or recorded_actual
            if adjustment is None and requested_actual != recorded_actual:
                adjustment_kind = (
                    "adjusted_up" if requested_actual > recorded_actual else "adjusted_down"
                )
                adjustment_entry_id = uuid.uuid4()
                adjustment_reason = (
                    f"late actual cost {requested_actual} {reserved['unit']} adjusted "
                    f"the reconciled estimate {recorded_actual} {reserved['unit']}"
                )
                adjustment_row = connection.execute(
                    budget_ledger.insert()
                    .values(
                        entry_id=adjustment_entry_id,
                        campaign_id=reserved["campaign_id"],
                        work_item_id=reserved["work_item_id"],
                        job_id=reserved["job_id"],
                        attempt_id=attempt_id,
                        lease_generation=reserved["lease_generation"],
                        reservation_entry_id=reservation_id,
                        kind=adjustment_kind,
                        amount=abs(requested_actual - recorded_actual),
                        unit=reserved["unit"],
                        reason=adjustment_reason,
                        recorded_at=func.now(),
                    )
                    .returning(budget_ledger.c.recorded_at)
                ).one()
                _append_budget_event(
                    connection,
                    entry_id=adjustment_entry_id,
                    campaign_id=reserved["campaign_id"],
                    work_item_id=reserved["work_item_id"],
                    job_id=reserved["job_id"],
                    attempt_id=attempt_id,
                    lease_generation=int(reserved["lease_generation"]),
                    reservation_entry_id=reservation_id,
                    kind=adjustment_kind,
                    amount=abs(requested_actual - recorded_actual),
                    unit=reserved["unit"],
                    reason=adjustment_reason,
                    recorded_at=adjustment_row.recorded_at,
                )
                recorded_actual = requested_actual
            elif requested_actual != recorded_actual:
                raise ReservationSettledError(reservation_id)
            if recorded_actual > Decimal(reserved["amount"]):
                exhaust_campaign(
                    connection,
                    reserved["campaign_id"],
                    reason=(
                        f"actual cost {recorded_actual} {reserved['unit']} exceeded the reserved "
                        f"estimate {reserved['amount']} {reserved['unit']}; "
                        "no further work may start"
                    ),
                )
            return Settlement(
                entry_id=existing["entry_id"],
                reservation_entry_id=reservation_id,
                kind=kind,
                amount=Decimal(existing["amount"]),
                unit=existing["unit"],
                reserved_amount=Decimal(reserved["amount"]),
                actual_amount=recorded_actual,
            )
        raise ReservationSettledError(reservation_id)
    entry_id = uuid.uuid4()
    settled_amount = (
        actual_amount
        if kind == "consumed" and actual_amount is not None
        else Decimal(reserved["amount"])
    )
    if settled_amount <= 0:
        raise ValueError("actual budget cost must be positive")
    settlement_row = connection.execute(
        budget_ledger.insert()
        .values(
            entry_id=entry_id,
            campaign_id=reserved["campaign_id"],
            work_item_id=reserved["work_item_id"],
            job_id=reserved["job_id"],
            attempt_id=attempt_id,
            lease_generation=reserved["lease_generation"],
            reservation_entry_id=reservation_id,
            kind=kind,
            amount=settled_amount,
            unit=reserved["unit"],
            reason=reason,
            recorded_at=func.now(),
        )
        .returning(budget_ledger.c.recorded_at)
    ).one()
    _append_budget_event(
        connection,
        entry_id=entry_id,
        campaign_id=reserved["campaign_id"],
        work_item_id=reserved["work_item_id"],
        job_id=reserved["job_id"],
        attempt_id=attempt_id,
        lease_generation=int(reserved["lease_generation"]),
        reservation_entry_id=reservation_id,
        kind=kind,
        amount=settled_amount,
        unit=reserved["unit"],
        reason=reason,
        recorded_at=settlement_row.recorded_at,
    )
    settlement = Settlement(
        entry_id=entry_id,
        reservation_entry_id=reservation_id,
        kind=kind,
        amount=settled_amount,
        unit=reserved["unit"],
        reserved_amount=Decimal(reserved["amount"]),
        actual_amount=settled_amount if kind == "consumed" else None,
    )
    if kind == "consumed" and settled_amount > Decimal(reserved["amount"]):
        exhaust_campaign(
            connection,
            reserved["campaign_id"],
            reason=(
                f"actual cost {settled_amount} {reserved['unit']} exceeded the reserved estimate "
                f"{reserved['amount']} {reserved['unit']}; no further work may start"
            ),
        )
    return settlement


def consume(
    connection: Connection,
    reservation_id: uuid.UUID,
    *,
    attempt_id: uuid.UUID,
    actual_amount: Decimal | None = None,
) -> Settlement:
    return _settle(
        connection,
        reservation_id,
        kind="consumed",
        reason="the reserved execution reached a durable attempt outcome",
        attempt_id=attempt_id,
        actual_amount=actual_amount,
    )


def release(
    connection: Connection,
    reservation_id: uuid.UUID,
    *,
    reason: str,
    attempt_id: uuid.UUID | None = None,
) -> Settlement:
    return _settle(
        connection,
        reservation_id,
        kind="released",
        reason=reason,
        attempt_id=attempt_id,
    )


def consume_outstanding_for_attempt(
    connection: Connection, *, job_id: uuid.UUID, attempt_id: uuid.UUID
) -> Settlement | None:
    reservation_id = connection.execute(
        select(budget_ledger.c.entry_id)
        .where(
            budget_ledger.c.job_id == job_id,
            budget_ledger.c.kind == "reserved",
            ~budget_ledger.c.entry_id.in_(
                select(budget_ledger.c.reservation_entry_id).where(
                    budget_ledger.c.reservation_entry_id.is_not(None)
                )
            ),
        )
        .order_by(budget_ledger.c.lease_generation.desc())
        .limit(1)
    ).scalar_one_or_none()
    if reservation_id is None:
        return None
    return consume(connection, reservation_id, attempt_id=attempt_id)


def release_outstanding_for_attempt(
    connection: Connection, *, job_id: uuid.UUID, attempt_id: uuid.UUID
) -> Settlement | None:
    reservation_id = connection.execute(
        select(budget_ledger.c.entry_id)
        .where(
            budget_ledger.c.job_id == job_id,
            budget_ledger.c.kind == "reserved",
            ~budget_ledger.c.entry_id.in_(
                select(budget_ledger.c.reservation_entry_id).where(
                    budget_ledger.c.reservation_entry_id.is_not(None)
                )
            ),
        )
        .order_by(budget_ledger.c.lease_generation.desc())
        .limit(1)
    ).scalar_one_or_none()
    if reservation_id is None:
        return None
    return release(
        connection,
        reservation_id,
        attempt_id=attempt_id,
        reason="the worker stopped before the durable adapter-start boundary",
    )


def release_outstanding_without_attempt(
    connection: Connection, *, job_id: uuid.UUID
) -> Settlement | None:
    reservation_id = connection.execute(
        select(budget_ledger.c.entry_id)
        .where(
            budget_ledger.c.job_id == job_id,
            budget_ledger.c.kind == "reserved",
            ~budget_ledger.c.entry_id.in_(
                select(budget_ledger.c.reservation_entry_id).where(
                    budget_ledger.c.reservation_entry_id.is_not(None)
                )
            ),
        )
        .order_by(budget_ledger.c.lease_generation.desc())
        .limit(1)
    ).scalar_one_or_none()
    if reservation_id is None:
        return None
    return release(
        connection,
        reservation_id,
        reason="the lease ended before an attempt reached the adapter-start boundary",
    )


class PostgresCampaignControlRepository(CampaignControlRepository):
    """Persist one control command and its projection in a single transaction."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def control(
        self,
        campaign_id: uuid.UUID,
        *,
        action: CampaignAction,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
    ) -> CampaignControlResult:
        scope = f"campaigns.{action}"
        target: dict[CampaignAction, tuple[CampaignState, str]] = {
            "pause": ("paused", "paused"),
            "resume": ("active", "resumed"),
            "cancel": ("cancelled", "cancelled"),
        }
        target_state, event_name = target[action]
        with self._engine.begin() as connection:
            inserted = connection.execute(
                pg_insert(idempotency_keys)
                .values(
                    scope=scope,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    campaign_id=campaign_id,
                    response=None,
                    created_at=func.now(),
                )
                .on_conflict_do_nothing(index_elements=["scope", "idempotency_key"])
                .returning(idempotency_keys.c.idempotency_key)
            ).one_or_none()
            if inserted is None:
                stored = connection.execute(
                    select(
                        idempotency_keys.c.request_hash,
                        idempotency_keys.c.campaign_id,
                        idempotency_keys.c.response,
                    ).where(
                        idempotency_keys.c.scope == scope,
                        idempotency_keys.c.idempotency_key == idempotency_key,
                    )
                ).one()
                check_request_fingerprint(
                    key=idempotency_key,
                    stored=stored.request_hash,
                    offered=request_hash,
                )
                if stored.campaign_id != campaign_id or stored.response is None:
                    raise IdempotencyConflictError(
                        idempotency_key,
                        stored=stored.request_hash,
                        offered=request_hash,
                    )
                return CampaignControlResult(
                    campaign_id=campaign_id,
                    state=stored.response["state"],
                    version=int(stored.response["version"]),
                    replayed=True,
                )

            row = connection.execute(
                select(campaigns.c.state)
                .where(campaigns.c.campaign_id == campaign_id)
                .with_for_update()
            ).one_or_none()
            if row is None:
                raise CampaignNotFoundError(campaign_id)
            actual = current_sequence(
                connection,
                campaign_id=campaign_id,
                aggregate_type="campaign",
                aggregate_id=campaign_id,
            )
            if actual != expected_version:
                raise ExpectedVersionError(campaign_id, expected_version, actual)
            check_campaign_transition(row.state, target_state)
            connection.execute(
                update(campaigns)
                .where(campaigns.c.campaign_id == campaign_id, campaigns.c.state == row.state)
                .values(state=target_state, updated_at=func.now())
            )
            version = _campaign_control_event(
                connection,
                campaign_id,
                event_name=event_name,
                state=target_state,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
            )
            if action == "cancel":
                from labbridge.runtime.jobs import cancel_available_for_campaign  # noqa: PLC0415

                cancel_available_for_campaign(
                    connection,
                    campaign_id,
                    causation_id=_latest_campaign_event(connection, campaign_id),
                    reason="campaign cancelled by explicit user command",
                )
            response = {"state": target_state, "version": version}
            connection.execute(
                update(idempotency_keys)
                .where(
                    idempotency_keys.c.scope == scope,
                    idempotency_keys.c.idempotency_key == idempotency_key,
                )
                .values(response=response)
            )
            return CampaignControlResult(
                campaign_id=campaign_id,
                state=target_state,
                version=version,
                replayed=False,
            )


__all__ = [
    "BudgetError",
    "BudgetUsage",
    "CampaignNotFoundError",
    "PostgresCampaignControlRepository",
    "Reservation",
    "ReservationSettledError",
    "Settlement",
    "budget_usage",
    "consume",
    "consume_outstanding_for_attempt",
    "exhaust_campaign",
    "release",
    "release_outstanding_for_attempt",
    "release_outstanding_without_attempt",
    "reserve",
]

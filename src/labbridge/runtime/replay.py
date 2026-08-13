# mypy: disable-error-code="call-overload,unused-ignore"
"""Deterministic reconstruction of campaign logical state from recorded events."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar

from pydantic import AwareDatetime, BaseModel, ConfigDict
from sqlalchemy import Connection, select, update

from labbridge.domain.events import EventEnvelope
from labbridge.domain.lifecycle import (
    ATTEMPT_TRANSITIONS,
    CAMPAIGN_TRANSITIONS,
    JOB_TRANSITIONS,
    WORK_ITEM_TRANSITIONS,
)
from labbridge.infrastructure.persistence.tables import (
    attempt_outcomes,
    attempts,
    budget_ledger,
    campaigns,
    jobs,
    observations,
    work_items,
)
from labbridge.runtime.events import load_replay_stream

REPLAY_COMPLETE_CONTRACT_VERSION = 2


class SemanticIncompleteEventStreamError(RuntimeError):
    """A valid envelope sequence omits a fact required by its later events."""

    code: ClassVar[str] = "semantic_incomplete_event_stream"


class _Projection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CampaignProjection(_Projection):
    campaign_id: uuid.UUID
    name: str
    environment_id: str
    adapter_version: str
    data_origin: str
    execution_mode: str
    declaration: dict[str, object]
    declaration_hash: str
    state: str
    created_at: AwareDatetime
    updated_at: AwareDatetime


class WorkItemProjection(_Projection):
    work_item_id: uuid.UUID
    campaign_id: uuid.UUID
    candidate_id: str
    candidate: dict[str, object]
    state: str
    quarantine_reason: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class JobProjection(_Projection):
    job_id: uuid.UUID
    work_item_id: uuid.UUID
    state: str
    available_at: AwareDatetime
    lease_owner: str | None
    lease_token: uuid.UUID | None
    lease_expires_at: AwareDatetime | None
    heartbeat_at: AwareDatetime | None
    lease_generation: int
    attempt_count: int
    max_attempts: int
    command_version: str
    idempotency_key: str
    last_failure: dict[str, object] | None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    event_correlation_id: uuid.UUID
    last_event_id: uuid.UUID


class AttemptOutcomeProjection(_Projection):
    attempt_id: uuid.UUID
    work_item_id: uuid.UUID
    campaign_id: uuid.UUID
    status: str
    observation_id: str | None
    failure: dict[str, object] | None
    cost: dict[str, object]
    data_origin: str
    execution_mode: str
    provenance: dict[str, object]
    started_at: AwareDatetime | None
    finished_at: AwareDatetime


class AttemptProjection(_Projection):
    attempt_id: uuid.UUID
    work_item_id: uuid.UUID
    job_id: uuid.UUID | None
    ordinal: int
    state: str
    started_at: AwareDatetime | None
    created_at: AwareDatetime
    outcome: AttemptOutcomeProjection | None


class ObservationProjection(_Projection):
    observation_id: str
    campaign_id: uuid.UUID
    work_item_id: uuid.UUID
    attempt_id: uuid.UUID
    media_type: str
    object_uri: str
    byte_size: int
    sha256: str
    schema_version: str
    signal_kind: str
    quantities: tuple[dict[str, object], ...]
    status: str
    status_reason: str | None
    data_origin: str
    execution_mode: str
    provenance: dict[str, object]
    received_at: AwareDatetime


class BudgetProjection(_Projection):
    hard_limit: Decimal | None
    unit: str
    reserved: Decimal
    consumed: Decimal
    released: Decimal
    adjusted_up: Decimal
    adjusted_down: Decimal
    outstanding: Decimal
    remaining: Decimal | None


class CampaignReplay(_Projection):
    campaign: CampaignProjection
    work_items: tuple[WorkItemProjection, ...]
    jobs: tuple[JobProjection, ...]
    attempts: tuple[AttemptProjection, ...]
    observations: tuple[ObservationProjection, ...]
    budget: BudgetProjection


class ProjectionMismatch(_Projection):
    path: str
    replayed: object
    persisted: object


class ReplayComparison(_Projection):
    campaign_id: uuid.UUID
    matches: bool
    mismatches: tuple[ProjectionMismatch, ...]


class ProjectionMismatchError(RuntimeError):
    """Persisted mutable state differs from the authoritative event fold."""

    code: ClassVar[str] = "projection_mismatch"

    def __init__(self, report: ReplayComparison) -> None:
        self.report = report
        super().__init__(
            f"campaign {report.campaign_id} has {len(report.mismatches)} projection mismatches"
        )


class NonRebuildableProjectionError(RuntimeError):
    """Persisted state diverges in a field replay must never overwrite or delete."""

    code: ClassVar[str] = "non_rebuildable_projection"

    def __init__(self, campaign_id: uuid.UUID, paths: tuple[str, ...]) -> None:
        self.campaign_id = campaign_id
        self.paths = paths
        super().__init__(
            f"campaign {campaign_id} has non-rebuildable projection differences: {', '.join(paths)}"
        )


REBUILDABLE_PROJECTION_FIELDS = (
    "campaign.mutable_fields_on_existing_row",
    "work_items.projection_row",
    "jobs.projection_row",
    "attempts.projection_row",
)


@dataclass(frozen=True)
class _BudgetEntry:
    kind: str
    amount: Decimal
    reservation_id: uuid.UUID | None
    work_item_id: uuid.UUID
    job_id: uuid.UUID
    attempt_id: uuid.UUID | None
    lease_generation: int
    unit: str


def _missing(message: str) -> SemanticIncompleteEventStreamError:
    return SemanticIncompleteEventStreamError(message)


def _budget_declaration(declaration: dict[str, object]) -> tuple[Decimal | None, str | None]:
    value = declaration.get("budget")
    if not isinstance(value, dict):
        return None, None
    hard_budget = value.get("hard_budget")
    unit = value.get("budget_unit")
    return (
        Decimal(str(hard_budget)) if hard_budget is not None else None,
        str(unit) if unit is not None else None,
    )


def reconstruct_campaign(  # noqa: PLR0912, PLR0915
    stream: tuple[EventEnvelope, ...], *, contract_version: int
) -> CampaignReplay:
    """Fold one already validated campaign stream without consulting mutable state."""
    if contract_version != REPLAY_COMPLETE_CONTRACT_VERSION:
        raise _missing(
            f"full campaign reconstruction requires event-stream contract version 2, got "
            f"{contract_version}"
        )

    campaign: CampaignProjection | None = None
    items: dict[uuid.UUID, WorkItemProjection] = {}
    job_rows: dict[uuid.UUID, JobProjection] = {}
    attempt_rows: dict[uuid.UUID, AttemptProjection] = {}
    observation_rows: dict[tuple[str, uuid.UUID], ObservationProjection] = {}
    budget_entries: dict[uuid.UUID, _BudgetEntry] = {}
    reservation_settlements: dict[uuid.UUID, str] = {}

    for event in stream:
        payload = event.payload
        if campaign is not None and event.campaign_id != campaign.campaign_id:
            raise _missing(f"event {event.event_id} belongs to another campaign")
        if event.event_type == "campaign.created":
            if campaign is not None:
                raise _missing("campaign.created occurs more than once")
            if event.aggregate_id != event.campaign_id:
                raise _missing("campaign.created aggregate identity differs from campaign identity")
            campaign = CampaignProjection(
                campaign_id=event.aggregate_id,
                name=str(payload["name"]),
                environment_id=str(payload["environment_id"]),
                adapter_version=str(payload["adapter_version"]),
                data_origin=str(payload["data_origin"]),
                execution_mode=str(payload["execution_mode"]),
                declaration=dict(payload["declaration"]),  # type: ignore[arg-type]
                declaration_hash=str(payload["declaration_hash"]),
                state=str(payload["state"]),
                created_at=event.occurred_at,
                updated_at=event.occurred_at,
            )
        elif event.event_type.startswith("campaign."):
            if campaign is None:
                raise _missing(f"{event.event_type} occurs before campaign.created")
            target_state = str(payload["state"])
            if target_state not in CAMPAIGN_TRANSITIONS.get(  # type: ignore[arg-type]
                campaign.state, frozenset()
            ):
                raise _missing(f"illegal campaign transition {campaign.state} -> {target_state}")
            campaign = campaign.model_copy(
                update={"state": target_state, "updated_at": event.occurred_at}
            )
        elif event.event_type == "work_item.queued":
            if campaign is None:
                raise _missing("work_item.queued occurs before campaign.created")
            if event.aggregate_id in items:
                raise _missing(f"work item {event.aggregate_id} is queued more than once")
            items[event.aggregate_id] = WorkItemProjection(
                work_item_id=event.aggregate_id,
                campaign_id=event.campaign_id,
                candidate_id=str(payload["candidate_id"]),
                candidate=dict(payload["candidate"]),  # type: ignore[arg-type]
                state=str(payload["state"]),
                quarantine_reason=None,
                created_at=event.occurred_at,
                updated_at=event.occurred_at,
            )
        elif event.event_type.startswith("work_item."):
            item = items.get(event.aggregate_id)
            if item is None:
                raise _missing(f"{event.event_type} has no work_item.queued event")
            target_state = str(payload["state"])
            if target_state not in WORK_ITEM_TRANSITIONS.get(  # type: ignore[arg-type]
                item.state, frozenset()
            ):
                raise _missing(f"illegal work-item transition {item.state} -> {target_state}")
            reason = payload.get("reason")
            items[event.aggregate_id] = item.model_copy(
                update={
                    "state": target_state,
                    "quarantine_reason": (
                        str(reason) if payload["state"] == "quarantined" else None
                    ),
                    "updated_at": event.occurred_at,
                }
            )
        elif event.event_type.startswith("job."):
            work_item_id = uuid.UUID(str(payload["work_item_id"]))
            if work_item_id not in items:
                raise _missing(f"job {event.aggregate_id} names absent work item {work_item_id}")
            prior_job = job_rows.get(event.aggregate_id)
            if prior_job is None and event.event_type != "job.enqueued":
                raise _missing(f"{event.event_type} has no job.enqueued event")
            if prior_job is not None and event.event_type == "job.enqueued":
                raise _missing(f"job {event.aggregate_id} is enqueued more than once")
            if payload.get("lease_generation") is None:
                raise _missing(f"job {event.aggregate_id} omits lease_generation")
            lease_generation = int(payload["lease_generation"])
            target_state = str(payload["state"])
            if prior_job is not None:
                if work_item_id != prior_job.work_item_id:
                    raise _missing(f"job {event.aggregate_id} changes work-item identity")
                for field in (
                    "max_attempts",
                    "command_version",
                    "idempotency_key",
                ):
                    if payload[field] != getattr(prior_job, field):
                        raise _missing(f"job {event.aggregate_id} changes immutable field {field}")
                if event.event_type in ("job.heartbeat", "job.lease_expired"):
                    if target_state != prior_job.state:
                        raise _missing(
                            f"{event.event_type} changes state {prior_job.state} -> {target_state}"
                        )
                elif target_state not in JOB_TRANSITIONS.get(  # type: ignore[arg-type]
                    prior_job.state, frozenset()
                ):
                    raise _missing(f"illegal job transition {prior_job.state} -> {target_state}")
                if lease_generation < prior_job.lease_generation:
                    raise _missing(f"job {event.aggregate_id} decreases lease_generation")
            elif target_state != "available" or lease_generation != 0:
                raise _missing("job.enqueued must establish available generation zero")
            next_job = JobProjection(
                job_id=event.aggregate_id,
                work_item_id=work_item_id,
                state=str(payload["state"]),
                available_at=payload["available_at"],  # type: ignore[arg-type]
                lease_owner=(str(payload["lease_owner"]) if payload.get("lease_owner") else None),
                lease_token=(
                    uuid.UUID(str(payload["lease_token"])) if payload.get("lease_token") else None
                ),
                lease_expires_at=payload.get("lease_expires_at"),  # type: ignore[arg-type]
                heartbeat_at=payload.get("heartbeat_at"),  # type: ignore[arg-type]
                lease_generation=lease_generation,
                attempt_count=int(payload["attempt_count"]),
                max_attempts=int(payload["max_attempts"]),
                command_version=str(payload["command_version"]),
                idempotency_key=str(payload["idempotency_key"]),
                last_failure=(
                    dict(payload["last_failure"])  # type: ignore[arg-type]
                    if payload.get("last_failure") is not None
                    else None
                ),
                created_at=payload["created_at"],  # type: ignore[arg-type]
                updated_at=payload["updated_at"],  # type: ignore[arg-type]
                event_correlation_id=event.correlation_id,
                last_event_id=event.event_id,
            )
            if prior_job is not None and next_job.created_at != prior_job.created_at:
                raise _missing(f"job {event.aggregate_id} changes immutable field created_at")
            job_rows[event.aggregate_id] = next_job
        elif event.event_type == "attempt.started":
            work_item_id = uuid.UUID(str(payload["work_item_id"]))
            job_id = uuid.UUID(str(payload["job_id"])) if payload.get("job_id") else None
            if work_item_id not in items or (job_id is not None and job_id not in job_rows):
                raise _missing(f"attempt {event.aggregate_id} has no complete work/job history")
            if event.aggregate_id in attempt_rows:
                raise _missing(f"attempt {event.aggregate_id} starts more than once")
            if job_id is None or job_rows[job_id].work_item_id != work_item_id:
                raise _missing(f"attempt {event.aggregate_id} has inconsistent work/job identity")
            attempt_rows[event.aggregate_id] = AttemptProjection(
                attempt_id=event.aggregate_id,
                work_item_id=work_item_id,
                job_id=job_id,
                ordinal=int(payload["ordinal"]),
                state=str(payload["state"]),
                started_at=payload.get("started_at"),  # type: ignore[arg-type]
                created_at=payload["created_at"],  # type: ignore[arg-type]
                outcome=None,
            )
        elif event.event_type == "attempt.completed":
            attempt = attempt_rows.get(event.aggregate_id)
            if attempt is None:
                raise _missing(f"attempt {event.aggregate_id} completed without attempt.started")
            if attempt.outcome is not None:
                raise _missing(f"attempt {event.aggregate_id} has more than one outcome")
            completed_work_item_id = uuid.UUID(str(payload["work_item_id"]))
            completed_job_id = (
                uuid.UUID(str(payload["job_id"])) if payload.get("job_id") is not None else None
            )
            completed_ordinal = (
                int(payload["ordinal"]) if payload.get("ordinal") is not None else None
            )
            if (
                completed_work_item_id != attempt.work_item_id
                or completed_job_id != attempt.job_id
                or completed_ordinal != attempt.ordinal
            ):
                raise _missing(
                    f"attempt {event.aggregate_id} completion changes execution identity"
                )
            if uuid.UUID(str(payload["campaign_id"])) != event.campaign_id:
                raise _missing(f"attempt {event.aggregate_id} completion changes campaign identity")
            target_state = str(payload["state"])
            if target_state not in ATTEMPT_TRANSITIONS.get(  # type: ignore[arg-type]
                attempt.state, frozenset()
            ):
                raise _missing(f"illegal attempt transition {attempt.state} -> {target_state}")
            outcome = AttemptOutcomeProjection(
                attempt_id=event.aggregate_id,
                work_item_id=completed_work_item_id,
                campaign_id=uuid.UUID(str(payload["campaign_id"])),
                status=str(payload["status"]),
                observation_id=(
                    str(payload["observation_id"])
                    if payload.get("observation_id") is not None
                    else None
                ),
                failure=(
                    dict(payload["failure"])  # type: ignore[arg-type]
                    if payload.get("failure") is not None
                    else None
                ),
                cost=dict(payload["cost"]),  # type: ignore[arg-type]
                data_origin=str(payload["data_origin"]),
                execution_mode=str(payload["execution_mode"]),
                provenance=dict(payload["provenance"]),  # type: ignore[arg-type]
                started_at=payload.get("started_at"),  # type: ignore[arg-type]
                finished_at=payload["finished_at"],  # type: ignore[arg-type]
            )
            attempt_rows[event.aggregate_id] = attempt.model_copy(
                update={"state": target_state, "outcome": outcome}
            )
        elif event.event_type in ("observation.accepted", "observation.retained"):
            attempt_id = uuid.UUID(str(payload["attempt_id"]))
            work_item_id = uuid.UUID(str(payload["work_item_id"]))
            if attempt_id not in attempt_rows or work_item_id not in items:
                raise _missing(
                    f"observation {payload['observation_id']} has no attempt/work history"
                )
            if event.aggregate_id != attempt_id:
                raise _missing(f"observation {payload['observation_id']} changes attempt identity")
            if attempt_rows[attempt_id].work_item_id != work_item_id:
                raise _missing(
                    f"observation {payload['observation_id']} changes work-item identity"
                )
            observation = ObservationProjection(
                observation_id=str(payload["observation_id"]),
                campaign_id=event.campaign_id,
                work_item_id=work_item_id,
                attempt_id=attempt_id,
                media_type=str(payload["media_type"]),
                object_uri=str(payload["object_uri"]),
                byte_size=int(payload["byte_size"]),
                sha256=str(payload["sha256"]),
                schema_version=str(payload["schema_version"]),
                signal_kind=str(payload["signal_kind"]),
                quantities=tuple(payload["quantities"]),  # type: ignore[arg-type]
                status=str(payload["status"]),
                status_reason=(
                    str(payload["status_reason"])
                    if payload.get("status_reason") is not None
                    else None
                ),
                data_origin=str(payload["data_origin"]),
                execution_mode=str(payload["execution_mode"]),
                provenance=dict(payload["provenance"]),  # type: ignore[arg-type]
                received_at=payload["received_at"],  # type: ignore[arg-type]
            )
            observation_key = (observation.observation_id, attempt_id)
            if observation_key in observation_rows:
                raise _missing(
                    f"observation {observation.observation_id} is recorded more than once"
                )
            observation_rows[observation_key] = observation
        elif event.event_type.startswith("budget."):
            entry_id = uuid.UUID(str(payload["entry_id"]))
            reservation_id = (
                uuid.UUID(str(payload["reservation_entry_id"]))
                if payload.get("reservation_entry_id")
                else None
            )
            kind = str(payload["kind"])
            work_item_id = uuid.UUID(str(payload["work_item_id"]))
            job_id = uuid.UUID(str(payload["job_id"]))
            budget_attempt_id = (
                uuid.UUID(str(payload["attempt_id"]))
                if payload.get("attempt_id") is not None
                else None
            )
            lease_generation = int(payload["lease_generation"])
            if entry_id in budget_entries:
                raise _missing(f"budget entry {entry_id} is recorded more than once")
            if event.aggregate_id != (reservation_id or entry_id):
                raise _missing(f"budget entry {entry_id} has inconsistent aggregate identity")
            job = job_rows.get(job_id)
            if campaign is None or work_item_id not in items or job is None:
                raise _missing(f"budget entry {entry_id} has no campaign/work/job history")
            if job.work_item_id != work_item_id:
                raise _missing(f"budget entry {entry_id} changes job/work identity")
            if kind == "reserved":
                if reservation_id is not None or budget_attempt_id is not None:
                    raise _missing(f"budget reservation {entry_id} has invalid identity shape")
                if lease_generation != job.lease_generation + 1:
                    raise _missing(f"budget reservation {entry_id} has invalid lease generation")
            else:
                if reservation_id is None:
                    raise _missing(f"budget entry {entry_id} omits its reservation identity")
                reservation = budget_entries.get(reservation_id)
                if reservation is None or reservation.kind != "reserved":
                    raise _missing(f"budget entry {entry_id} has no reservation {reservation_id}")
                if (
                    reservation.work_item_id != work_item_id
                    or reservation.job_id != job_id
                    or reservation.lease_generation != lease_generation
                    or reservation.unit != str(payload["unit"])
                ):
                    raise _missing(f"budget entry {entry_id} changes reservation identity")
                if budget_attempt_id is not None:
                    attempt = attempt_rows.get(budget_attempt_id)
                    if (
                        attempt is None
                        or attempt.work_item_id != work_item_id
                        or attempt.job_id != job_id
                    ):
                        raise _missing(f"budget entry {entry_id} names an unknown execution")
                if kind in ("consumed", "released"):
                    if reservation_id in reservation_settlements:
                        raise _missing(
                            f"budget reservation {reservation_id} is settled more than once"
                        )
                    reservation_settlements[reservation_id] = kind
                elif kind in ("adjusted_up", "adjusted_down"):
                    if reservation_settlements.get(reservation_id) != "consumed":
                        raise _missing(f"budget adjustment {entry_id} has no consumed settlement")
                    if any(
                        entry.reservation_id == reservation_id
                        and entry.kind in ("adjusted_up", "adjusted_down")
                        for entry in budget_entries.values()
                    ):
                        raise _missing(
                            f"budget reservation {reservation_id} is adjusted more than once"
                        )
            budget_entries[entry_id] = _BudgetEntry(
                kind=kind,
                amount=Decimal(str(payload["amount"])),
                reservation_id=reservation_id,
                work_item_id=work_item_id,
                job_id=job_id,
                attempt_id=budget_attempt_id,
                lease_generation=lease_generation,
                unit=str(payload["unit"]),
            )
        elif event.event_type in ("observation.invalidated", "observation.superseded"):
            continue
        else:
            raise _missing(f"replay has no fold rule for registered event {event.event_type}")

    if campaign is None:
        raise _missing("stream has no campaign.created event")
    succeeded_by_work_item: dict[uuid.UUID, int] = {}
    for attempt in attempt_rows.values():
        if attempt.outcome is None:
            if campaign.state in {"completed", "cancelled", "failed", "budget_exhausted"}:
                raise _missing(f"terminal campaign has open attempt {attempt.attempt_id}")
            continue
        observation_id = attempt.outcome.observation_id
        linked_observation = (
            observation_rows.get((observation_id, attempt.attempt_id))
            if observation_id is not None
            else None
        )
        if observation_id is not None and linked_observation is None:
            raise _missing(
                f"attempt {attempt.attempt_id} references observation {observation_id} without "
                "a recorded observation event"
            )
        if attempt.outcome.status == "succeeded":
            if linked_observation is None or linked_observation.status != "accepted":
                raise _missing(
                    f"succeeded attempt {attempt.attempt_id} has no accepted observation"
                )
            succeeded_by_work_item[attempt.work_item_id] = (
                succeeded_by_work_item.get(attempt.work_item_id, 0) + 1
            )
        elif linked_observation is not None:
            if attempt.outcome.status not in ("duplicate_suppressed", "lease_lost"):
                raise _missing(
                    f"attempt {attempt.attempt_id} status {attempt.outcome.status} cannot "
                    "reference an observation"
                )
            if linked_observation.status == "accepted":
                raise _missing(
                    f"non-succeeded attempt {attempt.attempt_id} references accepted observation"
                )

    for item in items.values():
        succeeded_count = succeeded_by_work_item.get(item.work_item_id, 0)
        if item.state == "accepted" and succeeded_count != 1:
            raise _missing(
                f"accepted work item {item.work_item_id} has {succeeded_count} succeeded outcomes"
            )
    if campaign.state == "completed":
        nonterminal_items = [
            item.work_item_id
            for item in items.values()
            if WORK_ITEM_TRANSITIONS.get(item.state, frozenset())
        ]
        if nonterminal_items:
            raise _missing(
                "completed campaign has nonterminal work items: "
                f"{', '.join(map(str, sorted(nonterminal_items, key=str)))}"
            )

    reserved = sum(
        (entry.amount for entry in budget_entries.values() if entry.kind == "reserved"),
        Decimal(0),
    )
    gross_consumed = sum(
        (entry.amount for entry in budget_entries.values() if entry.kind == "consumed"),
        Decimal(0),
    )
    released = sum(
        (entry.amount for entry in budget_entries.values() if entry.kind == "released"), Decimal(0)
    )
    adjusted_up = sum(
        (entry.amount for entry in budget_entries.values() if entry.kind == "adjusted_up"),
        Decimal(0),
    )
    adjusted_down = sum(
        (entry.amount for entry in budget_entries.values() if entry.kind == "adjusted_down"),
        Decimal(0),
    )
    consumed = gross_consumed + adjusted_up - adjusted_down
    outstanding = sum(
        (
            entry.amount
            for entry_id, entry in budget_entries.items()
            if entry.kind == "reserved" and entry_id not in reservation_settlements
        ),
        Decimal(0),
    )
    units = {entry.unit for entry in budget_entries.values()}
    if len(units) > 1:
        raise _missing(f"budget stream mixes units: {sorted(units)}")
    hard_limit, declared_unit = _budget_declaration(campaign.declaration)
    unit = next(iter(units), declared_unit or "")
    if declared_unit is not None and unit and declared_unit != unit:
        raise _missing(f"budget unit {unit} differs from declared unit {declared_unit}")
    remaining = (
        max(hard_limit - consumed - outstanding, Decimal(0)) if hard_limit is not None else None
    )
    return CampaignReplay(
        campaign=campaign,
        work_items=tuple(items[key] for key in sorted(items, key=str)),
        jobs=tuple(job_rows[key] for key in sorted(job_rows, key=str)),
        attempts=tuple(attempt_rows[key] for key in sorted(attempt_rows, key=str)),
        observations=tuple(observation_rows[key] for key in sorted(observation_rows, key=str)),
        budget=BudgetProjection(
            hard_limit=hard_limit,
            unit=unit,
            reserved=reserved,
            consumed=consumed,
            released=released,
            adjusted_up=adjusted_up,
            adjusted_down=adjusted_down,
            outstanding=outstanding,
            remaining=remaining,
        ),
    )


def _persisted_campaign(connection: Connection, campaign_id: uuid.UUID) -> CampaignReplay:
    campaign_row = (
        connection.execute(select(campaigns).where(campaigns.c.campaign_id == campaign_id))
        .mappings()
        .one()
    )
    campaign = CampaignProjection.model_validate(
        {name: campaign_row[name] for name in CampaignProjection.model_fields}
    )
    item_rows = connection.execute(
        select(work_items)
        .where(work_items.c.campaign_id == campaign_id)
        .order_by(work_items.c.work_item_id)
    ).mappings()
    item_projections = tuple(
        WorkItemProjection.model_validate(
            {name: row[name] for name in WorkItemProjection.model_fields}
        )
        for row in item_rows
    )
    job_rows = connection.execute(
        select(jobs)
        .join(work_items, work_items.c.work_item_id == jobs.c.work_item_id)
        .where(work_items.c.campaign_id == campaign_id)
        .order_by(jobs.c.job_id)
    ).mappings()
    job_projections = tuple(
        JobProjection.model_validate({name: row[name] for name in JobProjection.model_fields})
        for row in job_rows
    )
    outcome_by_attempt = {
        row["attempt_id"]: row
        for row in connection.execute(
            select(attempt_outcomes).where(attempt_outcomes.c.campaign_id == campaign_id)
        ).mappings()
    }
    attempt_rows = connection.execute(
        select(attempts)
        .join(work_items, work_items.c.work_item_id == attempts.c.work_item_id)
        .where(work_items.c.campaign_id == campaign_id)
        .order_by(attempts.c.attempt_id)
    ).mappings()
    attempt_projections: list[AttemptProjection] = []
    for row in attempt_rows:
        outcome_row = outcome_by_attempt.get(row["attempt_id"])
        outcome = (
            AttemptOutcomeProjection.model_validate(
                {name: outcome_row[name] for name in AttemptOutcomeProjection.model_fields}
            )
            if outcome_row is not None
            else None
        )
        attempt_projections.append(
            AttemptProjection.model_validate(
                {
                    **{
                        name: row[name]
                        for name in AttemptProjection.model_fields
                        if name != "outcome"
                    },
                    "outcome": outcome,
                }
            )
        )
    observation_rows = connection.execute(
        select(observations)
        .where(observations.c.campaign_id == campaign_id)
        .order_by(observations.c.observation_id, observations.c.attempt_id)
    ).mappings()
    observation_projections = tuple(
        ObservationProjection.model_validate(
            {name: row[name] for name in ObservationProjection.model_fields}
        )
        for row in observation_rows
    )
    ledger = list(
        connection.execute(
            select(budget_ledger).where(budget_ledger.c.campaign_id == campaign_id)
        ).mappings()
    )
    settlements = {
        row["reservation_entry_id"]
        for row in ledger
        if row["reservation_entry_id"] is not None and row["kind"] in ("consumed", "released")
    }
    reserved = sum(
        (Decimal(row["amount"]) for row in ledger if row["kind"] == "reserved"), Decimal(0)
    )
    released = sum(
        (Decimal(row["amount"]) for row in ledger if row["kind"] == "released"), Decimal(0)
    )
    adjusted_up = sum(
        (Decimal(row["amount"]) for row in ledger if row["kind"] == "adjusted_up"), Decimal(0)
    )
    adjusted_down = sum(
        (Decimal(row["amount"]) for row in ledger if row["kind"] == "adjusted_down"), Decimal(0)
    )
    consumed = (
        sum((Decimal(row["amount"]) for row in ledger if row["kind"] == "consumed"), Decimal(0))
        + adjusted_up
        - adjusted_down
    )
    outstanding = sum(
        (
            Decimal(row["amount"])
            for row in ledger
            if row["kind"] == "reserved" and row["entry_id"] not in settlements
        ),
        Decimal(0),
    )
    hard_limit = Decimal(campaign_row["hard_budget"])
    budget = BudgetProjection(
        hard_limit=hard_limit,
        unit=campaign_row["budget_unit"],
        reserved=reserved,
        consumed=consumed,
        released=released,
        adjusted_up=adjusted_up,
        adjusted_down=adjusted_down,
        outstanding=outstanding,
        remaining=max(hard_limit - consumed - outstanding, Decimal(0)),
    )
    return CampaignReplay(
        campaign=campaign,
        work_items=item_projections,
        jobs=job_projections,
        attempts=tuple(attempt_projections),
        observations=observation_projections,
        budget=budget,
    )


def load_reconstructed_campaign(connection: Connection, campaign_id: uuid.UUID) -> CampaignReplay:
    contract_version = int(
        connection.execute(
            select(campaigns.c.event_stream_contract_version).where(
                campaigns.c.campaign_id == campaign_id
            )
        ).scalar_one()
    )
    return reconstruct_campaign(
        load_replay_stream(connection, campaign_id), contract_version=contract_version
    )


def _logical(value: BaseModel) -> object:
    return value.model_dump(mode="json")


def compare_campaign_projection(connection: Connection, campaign_id: uuid.UUID) -> ReplayComparison:
    replayed = load_reconstructed_campaign(connection, campaign_id)
    persisted = _persisted_campaign(connection, campaign_id)
    mismatches: list[ProjectionMismatch] = []
    for path, replay_value, persisted_value in (
        ("campaign", replayed.campaign, persisted.campaign),
        ("work_items", replayed.work_items, persisted.work_items),
        ("jobs", replayed.jobs, persisted.jobs),
        ("attempts", replayed.attempts, persisted.attempts),
        ("observations", replayed.observations, persisted.observations),
        ("budget", replayed.budget, persisted.budget),
    ):
        replay_logical = _logical_sequence(replay_value)
        persisted_logical = _logical_sequence(persisted_value)
        if replay_logical != persisted_logical:
            mismatches.append(
                ProjectionMismatch(path=path, replayed=replay_logical, persisted=persisted_logical)
            )
    return ReplayComparison(
        campaign_id=campaign_id, matches=not mismatches, mismatches=tuple(mismatches)
    )


def _logical_sequence(value: BaseModel | tuple[BaseModel, ...]) -> object:
    if isinstance(value, tuple):
        return [_logical(item) for item in value]
    return _logical(value)


def require_matching_projection(connection: Connection, campaign_id: uuid.UUID) -> CampaignReplay:
    report = compare_campaign_projection(connection, campaign_id)
    if not report.matches:
        raise ProjectionMismatchError(report)
    return load_reconstructed_campaign(connection, campaign_id)


def _non_rebuildable_paths(replayed: CampaignReplay, persisted: CampaignReplay) -> tuple[str, ...]:
    paths: list[str] = []
    for field in CampaignProjection.model_fields:
        if field not in {"state", "updated_at"} and getattr(replayed.campaign, field) != getattr(
            persisted.campaign, field
        ):
            paths.append(f"campaign.{field}")

    def compare_rows(
        name: str,
        replay_rows: tuple[_Projection, ...],
        persisted_rows: tuple[_Projection, ...],
        identity_field: str,
        immutable_fields: tuple[str, ...],
    ) -> None:
        expected = {getattr(row, identity_field): row for row in replay_rows}
        actual = {getattr(row, identity_field): row for row in persisted_rows}
        for identity in sorted(actual.keys() - expected.keys(), key=str):
            paths.append(f"{name}.extra[{identity}]")
        for identity in sorted(actual.keys() & expected.keys(), key=str):
            for field in immutable_fields:
                if getattr(expected[identity], field) != getattr(actual[identity], field):
                    paths.append(f"{name}[{identity}].{field}")

    compare_rows(
        "work_items",
        replayed.work_items,
        persisted.work_items,
        "work_item_id",
        ("campaign_id", "candidate_id", "candidate", "created_at"),
    )
    compare_rows(
        "jobs",
        replayed.jobs,
        persisted.jobs,
        "job_id",
        (
            "work_item_id",
            "max_attempts",
            "command_version",
            "idempotency_key",
            "created_at",
            "event_correlation_id",
        ),
    )
    compare_rows(
        "attempts",
        replayed.attempts,
        persisted.attempts,
        "attempt_id",
        ("work_item_id", "job_id", "ordinal", "started_at", "created_at", "outcome"),
    )
    if replayed.observations != persisted.observations:
        paths.append("observations")
    if replayed.budget != persisted.budget:
        paths.append("budget")
    return tuple(paths)


def _apply_mutable_rebuild(
    connection: Connection, campaign_id: uuid.UUID, replayed: CampaignReplay
) -> None:
    campaign_result = connection.execute(
        update(campaigns)
        .where(campaigns.c.campaign_id == campaign_id)
        .values(state=replayed.campaign.state, updated_at=replayed.campaign.updated_at)
    )
    if campaign_result.rowcount != 1:
        raise _missing(f"campaign projection {campaign_id} is missing and cannot be rebuilt safely")
    for item in replayed.work_items:
        result = connection.execute(
            update(work_items)
            .where(work_items.c.work_item_id == item.work_item_id)
            .values(
                state=item.state,
                quarantine_reason=item.quarantine_reason,
                updated_at=item.updated_at,
            )
        )
        if result.rowcount != 1:
            connection.execute(
                work_items.insert().values(
                    work_item_id=item.work_item_id,
                    campaign_id=item.campaign_id,
                    candidate_id=item.candidate_id,
                    candidate=item.candidate,
                    state=item.state,
                    quarantine_reason=item.quarantine_reason,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
            )
    for job in replayed.jobs:
        result = connection.execute(
            update(jobs)
            .where(jobs.c.job_id == job.job_id)
            .values(
                state=job.state,
                available_at=job.available_at,
                lease_owner=job.lease_owner,
                lease_token=job.lease_token,
                lease_expires_at=job.lease_expires_at,
                heartbeat_at=job.heartbeat_at,
                lease_generation=job.lease_generation,
                attempt_count=job.attempt_count,
                last_failure=job.last_failure,
                last_event_id=job.last_event_id,
                updated_at=job.updated_at,
            )
        )
        if result.rowcount != 1:
            connection.execute(
                jobs.insert().values(
                    job_id=job.job_id,
                    work_item_id=job.work_item_id,
                    state=job.state,
                    available_at=job.available_at,
                    lease_owner=job.lease_owner,
                    lease_token=job.lease_token,
                    lease_generation=job.lease_generation,
                    lease_expires_at=job.lease_expires_at,
                    heartbeat_at=job.heartbeat_at,
                    attempt_count=job.attempt_count,
                    max_attempts=job.max_attempts,
                    command_version=job.command_version,
                    idempotency_key=job.idempotency_key,
                    last_failure=job.last_failure,
                    event_correlation_id=job.event_correlation_id,
                    last_event_id=job.last_event_id,
                    created_at=job.created_at,
                    updated_at=job.updated_at,
                )
            )
    for attempt in replayed.attempts:
        result = connection.execute(
            update(attempts)
            .where(attempts.c.attempt_id == attempt.attempt_id)
            .values(state=attempt.state)
        )
        if result.rowcount != 1:
            connection.execute(
                attempts.insert().values(
                    attempt_id=attempt.attempt_id,
                    work_item_id=attempt.work_item_id,
                    job_id=attempt.job_id,
                    ordinal=attempt.ordinal,
                    state=attempt.state,
                    started_at=attempt.started_at,
                    adapter_started_at=None,
                    created_at=attempt.created_at,
                )
            )


def rebuild_mutable_projections(connection: Connection, campaign_id: uuid.UUID) -> CampaignReplay:
    """Restore mutable rows without writing evidence, ledger entries, or the campaign FK root.

    The campaign row is update-only because it carries the stream contract and is the restricted
    foreign-key root for its events. Work-item, job, and outcome-free attempt rows can be recreated
    in dependency order from a version-2 fold.
    """
    if (
        connection.execute(
            select(campaigns.c.campaign_id).where(campaigns.c.campaign_id == campaign_id)
        ).scalar_one_or_none()
        is None
    ):
        raise _missing(f"campaign projection {campaign_id} is missing and cannot be rebuilt safely")
    replayed = load_reconstructed_campaign(connection, campaign_id)
    non_rebuildable = _non_rebuildable_paths(replayed, _persisted_campaign(connection, campaign_id))
    if non_rebuildable:
        raise NonRebuildableProjectionError(campaign_id, non_rebuildable)
    with connection.begin_nested():
        _apply_mutable_rebuild(connection, campaign_id, replayed)
        report = compare_campaign_projection(connection, campaign_id)
        if not report.matches:
            raise ProjectionMismatchError(report)
    return replayed

"""The event envelope.

`docs/SPEC.md` §5.1. The requirements that shape this type: `sequence` is unique and monotonic per
aggregate, append uses an expected aggregate version, event types and payload versions are
registered, unsupported versions fail explicitly, aggregate streams use sequence, and campaign
streams use their explicit campaign-wide position rather than timestamps.

Only the first sequence number and the ordering rule live here. Uniqueness and atomic append are
database properties and are enforced there — a domain type cannot promise them, and claiming so in a
docstring would be the kind of overclaim `AI_CONTRACT.md` §10 forbids.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import ClassVar, Final, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from .candidates import HerCandidate
from .identity import DataOrigin, EnvironmentRef, ExecutionMode
from .lifecycle import AttemptState, CampaignState, JobState, WorkItemState
from .provenance import Provenance
from .quantities import CostRecord
from .results import (
    AttemptOutcome,
    AttemptStatus,
    FailureRecord,
    ObservationStatus,
    QuantityDescriptor,
    SignalKind,
)

#: Sequences start at 1, so 0 can mean "no event yet" in an expected-version check without an
#: extra sentinel type.
FIRST_SEQUENCE: Final = 1


class EventEnvelope(BaseModel):
    """One recorded fact about one aggregate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    campaign_id: UUID
    aggregate_id: UUID
    aggregate_type: str = Field(min_length=1)
    sequence: int = Field(ge=FIRST_SEQUENCE)
    campaign_position: int = Field(ge=FIRST_SEQUENCE)
    event_type: str = Field(min_length=1)
    schema_version: int = Field(ge=1)
    #: When the fact happened, per the producer.
    occurred_at: datetime
    #: When the store accepted it. Distinct from `occurred_at`: a late append is not a late event.
    recorded_at: datetime
    correlation_id: UUID
    causation_id: UUID | None = None
    idempotency_key: str | None = None
    payload: dict[str, object]

    @model_validator(mode="after")
    def _timestamps_must_be_aware(self) -> Self:
        """A naive timestamp means "some timezone", which replay cannot order across hosts."""
        for label in ("occurred_at", "recorded_at"):
            moment: datetime = getattr(self, label)
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ValueError(f"{label} must be timezone-aware")
        return self


class UnsupportedEventVersionError(ValueError):
    """An event whose schema version this build does not know how to read.

    Raised rather than coerced or defaulted: reading an unknown version with the closest known
    schema is how a replay silently produces wrong state.
    """

    code: ClassVar[str] = "unsupported_event_version"

    def __init__(self, event_type: str, schema_version: int, supported: frozenset[int]) -> None:
        self.event_type = event_type
        self.schema_version = schema_version
        self.supported = supported
        known = ", ".join(str(v) for v in sorted(supported)) or "none"
        super().__init__(
            f"event `{event_type}` schema version {schema_version} is not supported "
            f"(known versions: {known})"
        )


class UnknownEventTypeError(ValueError):
    """An event name this build does not recognize."""

    code: ClassVar[str] = "unknown_event_type"

    def __init__(self, event_type: str) -> None:
        self.event_type = event_type
        super().__init__(f"event type `{event_type}` is not registered")


class InvalidEventPayloadError(ValueError):
    """A registered event whose payload does not satisfy its versioned schema."""

    code: ClassVar[str] = "invalid_event_payload"

    def __init__(self, event_type: str, schema_version: int, error: ValidationError | str) -> None:
        self.event_type = event_type
        self.schema_version = schema_version
        self.validation_error = error
        super().__init__(
            f"invalid payload for `{event_type}` schema version {schema_version}: {error}"
        )


class EventAggregateTypeError(ValueError):
    """A registered event addressed to the wrong kind of aggregate."""

    code: ClassVar[str] = "event_aggregate_type_mismatch"

    def __init__(self, event_type: str, expected: str, actual: str) -> None:
        self.event_type = event_type
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"event `{event_type}` requires aggregate type `{expected}`, got `{actual}`"
        )


class MissingEventCausationError(ValueError):
    """A non-root event without the event that caused it."""

    code: ClassVar[str] = "event_causation_required"

    def __init__(self, event_type: str) -> None:
        self.event_type = event_type
        super().__init__(f"event `{event_type}` requires causation_id")


class _EventPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CampaignCreatedPayload(_EventPayload):
    name: str = Field(min_length=1)
    environment_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    data_origin: DataOrigin
    execution_mode: ExecutionMode
    declaration: dict[str, object]
    declaration_hash: str = Field(min_length=64, max_length=64)
    state: CampaignState

    @model_validator(mode="after")
    def _environment_pair_must_be_admissible(self) -> Self:
        EnvironmentRef(
            environment_id=self.environment_id,
            adapter_version=self.adapter_version,
            data_origin=self.data_origin,
            execution_mode=self.execution_mode,
        )
        return self


class CampaignStatePayload(_EventPayload):
    state: CampaignState
    reason: str | None = None

    @model_validator(mode="after")
    def _failure_states_why(self) -> Self:
        if self.state == "failed" and not self.reason:
            raise ValueError("a failed campaign requires a reason")
        return self


class WorkItemQueuedPayload(_EventPayload):
    candidate_id: str = Field(min_length=1)
    candidate: HerCandidate
    state: WorkItemState


class WorkItemStatePayload(_EventPayload):
    state: WorkItemState
    reason: str | None = None

    @model_validator(mode="after")
    def _rejection_or_quarantine_states_why(self) -> Self:
        if self.state in ("rejected", "quarantined") and not self.reason:
            raise ValueError(f"work item state `{self.state}` requires a reason")
        return self


class JobFailurePayload(_EventPayload):
    failure_code: str = Field(min_length=1)


class JobProjectionPayload(_EventPayload):
    work_item_id: UUID
    state: JobState
    available_at: AwareDatetime
    lease_owner: str | None = None
    lease_token: UUID | None = None
    lease_expires_at: AwareDatetime | None = None
    heartbeat_at: AwareDatetime | None = None
    lease_generation: int | None = Field(default=None, ge=0)
    attempt_count: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    command_version: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    last_failure: JobFailurePayload | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class AttemptStartedPayload(_EventPayload):
    work_item_id: UUID
    job_id: UUID | None = None
    ordinal: int = Field(ge=1)
    state: AttemptState
    started_at: AwareDatetime | None = None
    created_at: AwareDatetime


class AttemptCompletedPayload(_EventPayload):
    work_item_id: UUID
    job_id: UUID | None = None
    ordinal: int | None = Field(default=None, ge=1)
    campaign_id: UUID
    state: AttemptState
    status: AttemptStatus
    observation_id: str | None = None
    failure: FailureRecord | None = None
    cost: CostRecord
    data_origin: DataOrigin
    execution_mode: ExecutionMode
    provenance: Provenance
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime

    @model_validator(mode="after")
    def _outcome_is_internally_consistent(self) -> Self:
        AttemptOutcome(
            attempt_id="event-envelope",
            work_item_id=str(self.work_item_id),
            status=self.status,
            observation_id=self.observation_id,
            failure=self.failure,
            cost=self.cost,
            started_at=self.started_at,
            finished_at=self.finished_at,
            provenance=self.provenance,
        )
        if self.status == "succeeded" and self.observation_id is None:
            raise ValueError("a succeeded outcome requires its observation")
        environment = self.provenance.environment
        environment_mismatch = (self.data_origin, self.execution_mode) != (
            environment.data_origin,
            environment.execution_mode,
        )
        records_declared_environment_refusal = (
            self.observation_id is None
            and self.failure is not None
            and self.failure.failure_code == "environment_mismatch"
        )
        if environment_mismatch and not records_declared_environment_refusal:
            raise ValueError("outcome origin and mode must match its provenance environment")
        if self.observation_id is not None and not self.provenance.has_root:
            raise ValueError("an outcome with received bytes must resolve to one provenance root")
        return self


class ObservationRecordedPayload(_EventPayload):
    observation_id: str = Field(min_length=1)
    work_item_id: UUID
    attempt_id: UUID
    media_type: str = Field(min_length=1)
    object_uri: str = Field(min_length=1)
    byte_size: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)
    schema_version: str = Field(min_length=1)
    signal_kind: SignalKind
    quantities: tuple[QuantityDescriptor, ...]
    status: ObservationStatus
    status_reason: str | None = None
    data_origin: DataOrigin
    execution_mode: ExecutionMode
    provenance: Provenance
    received_at: AwareDatetime

    @model_validator(mode="after")
    def _observation_has_one_matching_root(self) -> Self:
        environment = self.provenance.environment
        if (self.data_origin, self.execution_mode) != (
            environment.data_origin,
            environment.execution_mode,
        ):
            raise ValueError("observation origin and mode must match its provenance environment")
        if not self.provenance.has_root:
            raise ValueError("an observation must resolve to one provenance root")
        return self


class ObservationAcceptedPayload(ObservationRecordedPayload):
    pass


class ObservationRetainedPayload(ObservationRecordedPayload):
    pass


class BudgetEntryPayload(_EventPayload):
    entry_id: UUID
    work_item_id: UUID
    job_id: UUID
    attempt_id: UUID | None = None
    lease_generation: int = Field(ge=1)
    reservation_entry_id: UUID | None = None
    kind: Literal["reserved", "consumed", "released", "adjusted_up", "adjusted_down"]
    amount: Decimal = Field(gt=0)
    unit: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    recorded_at: AwareDatetime

    @model_validator(mode="after")
    def _reservation_shape_matches_kind(self) -> Self:
        if self.kind == "reserved":
            if self.reservation_entry_id is not None or self.attempt_id is not None:
                raise ValueError(
                    "a reservation cannot settle another reservation or name an attempt"
                )
        elif self.reservation_entry_id is None:
            raise ValueError("a budget settlement or adjustment requires reservation_entry_id")
        return self


class ObservationRelationPayload(_EventPayload):
    relation_id: UUID
    subject_id: str = Field(min_length=1)
    predicate: Literal["invalidates", "supersedes"]
    object_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    recorded_at: AwareDatetime

    @model_validator(mode="after")
    def _relation_is_not_reflexive(self) -> Self:
        if self.subject_id == self.object_id:
            raise ValueError("an observation relation cannot refer to itself")
        return self


@dataclass(frozen=True)
class EventDefinition:
    payload_model: type[_EventPayload]
    aggregate_type: str
    causation_required: bool = True
    expected_states: frozenset[str] | None = None


def _definitions() -> dict[tuple[str, int], EventDefinition]:
    campaign_states = {
        "started": "active",
        "paused": "paused",
        "resumed": "active",
        "completed": "completed",
        "cancelled": "cancelled",
        "failed": "failed",
        "budget_exhausted": "budget_exhausted",
    }
    work_item_states = {name: name for name in ("accepted", "quarantined", "rejected", "cancelled")}
    job_states = {
        "enqueued": frozenset({"available"}),
        "leased": frozenset({"leased"}),
        "started": frozenset({"running"}),
        "heartbeat": frozenset({"leased", "running"}),
        "lease_expired": frozenset({"leased", "running"}),
        "available": frozenset({"available"}),
        "retry_scheduled": frozenset({"retry_scheduled"}),
        "succeeded": frozenset({"succeeded"}),
        "failed_terminal": frozenset({"failed_terminal"}),
        "timed_out": frozenset({"timed_out"}),
        "cancelled": frozenset({"cancelled"}),
    }
    definitions: dict[tuple[str, int], EventDefinition] = {
        ("campaign.created", 1): EventDefinition(
            CampaignCreatedPayload, "campaign", causation_required=False
        ),
        ("work_item.queued", 1): EventDefinition(
            WorkItemQueuedPayload, "work_item", expected_states=frozenset({"queued"})
        ),
        ("attempt.started", 1): EventDefinition(
            AttemptStartedPayload, "attempt", expected_states=frozenset({"running"})
        ),
        ("attempt.completed", 1): EventDefinition(AttemptCompletedPayload, "attempt"),
        ("observation.accepted", 1): EventDefinition(ObservationAcceptedPayload, "attempt"),
        ("observation.retained", 1): EventDefinition(ObservationRetainedPayload, "attempt"),
        ("budget.reserved", 1): EventDefinition(BudgetEntryPayload, "budget"),
        ("budget.consumed", 1): EventDefinition(BudgetEntryPayload, "budget"),
        ("budget.released", 1): EventDefinition(BudgetEntryPayload, "budget"),
        ("budget.adjusted", 1): EventDefinition(BudgetEntryPayload, "budget"),
        ("observation.invalidated", 1): EventDefinition(ObservationRelationPayload, "campaign"),
        ("observation.superseded", 1): EventDefinition(ObservationRelationPayload, "campaign"),
    }
    definitions.update(
        {
            (f"campaign.{name}", 1): EventDefinition(
                CampaignStatePayload, "campaign", expected_states=frozenset({state})
            )
            for name, state in campaign_states.items()
        }
    )
    definitions.update(
        {
            (f"work_item.{name}", 1): EventDefinition(
                WorkItemStatePayload, "work_item", expected_states=frozenset({state})
            )
            for name, state in work_item_states.items()
        }
    )
    definitions.update(
        {
            (f"job.{name}", 1): EventDefinition(JobProjectionPayload, "job", expected_states=states)
            for name, states in job_states.items()
        }
    )
    return definitions


EVENT_REGISTRY: Final = _definitions()


def registered_event_types() -> frozenset[tuple[str, int]]:
    """Return the event names and schema versions understood by this build."""
    return frozenset(EVENT_REGISTRY)


def validate_event_payload(  # noqa: PLR0912
    *,
    event_type: str,
    schema_version: int,
    aggregate_type: str,
    causation_id: UUID | None,
    payload: object,
) -> dict[str, object]:
    """Validate an append or loaded event against the exact registered schema."""
    supported = frozenset(version for name, version in EVENT_REGISTRY if name == event_type)
    if not supported:
        raise UnknownEventTypeError(event_type)
    definition = EVENT_REGISTRY.get((event_type, schema_version))
    if definition is None:
        raise UnsupportedEventVersionError(event_type, schema_version, supported)
    if aggregate_type != definition.aggregate_type:
        raise EventAggregateTypeError(event_type, definition.aggregate_type, aggregate_type)
    if definition.causation_required and causation_id is None:
        raise MissingEventCausationError(event_type)
    try:
        validated = definition.payload_model.model_validate(payload)
    except ValidationError as error:
        raise InvalidEventPayloadError(event_type, schema_version, error) from error
    state = getattr(validated, "state", None)
    if definition.expected_states is not None and state not in definition.expected_states:
        expected = ", ".join(sorted(definition.expected_states))
        raise InvalidEventPayloadError(
            event_type,
            schema_version,
            f"event requires state in {{{expected}}}, got `{state}`",
        )
    if isinstance(validated, ObservationAcceptedPayload) and validated.status != "accepted":
        raise InvalidEventPayloadError(
            event_type, schema_version, "observation.accepted requires status accepted"
        )
    if isinstance(validated, ObservationRetainedPayload) and validated.status not in (
        "received",
        "corrupted",
    ):
        raise InvalidEventPayloadError(
            event_type,
            schema_version,
            "observation.retained requires status received or corrupted",
        )
    if isinstance(validated, BudgetEntryPayload):
        expected_kind = {
            "budget.reserved": frozenset({"reserved"}),
            "budget.consumed": frozenset({"consumed"}),
            "budget.released": frozenset({"released"}),
            "budget.adjusted": frozenset({"adjusted_up", "adjusted_down"}),
        }[event_type]
        if validated.kind not in expected_kind:
            raise InvalidEventPayloadError(
                event_type,
                schema_version,
                f"event requires kind in {sorted(expected_kind)}, got `{validated.kind}`",
            )
    if isinstance(validated, ObservationRelationPayload):
        expected_predicate = {
            "observation.invalidated": "invalidates",
            "observation.superseded": "supersedes",
        }[event_type]
        if validated.predicate != expected_predicate:
            raise InvalidEventPayloadError(
                event_type,
                schema_version,
                f"event requires predicate `{expected_predicate}`, got `{validated.predicate}`",
            )
    if isinstance(validated, AttemptCompletedPayload):
        expected_state = validated.status
        if validated.state != expected_state:
            raise InvalidEventPayloadError(
                event_type,
                schema_version,
                f"attempt status `{validated.status}` requires state `{expected_state}`",
            )
    return validated.model_dump(mode="json")


def replay_order(events: tuple[EventEnvelope, ...]) -> tuple[EventEnvelope, ...]:
    """Order events by the campaign-wide position, never by timestamp.

    Two events can share a timestamp, and clocks disagree across hosts. The campaign position is the
    total order across aggregates; aggregate sequence is validated independently.
    """
    return tuple(
        sorted(events, key=lambda event: (str(event.campaign_id), event.campaign_position))
    )

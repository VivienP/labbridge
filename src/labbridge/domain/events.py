"""The event envelope.

`docs/SPEC.md` §5.1. The requirements that shape this type: `sequence` is unique and monotonic per
aggregate, append uses an expected aggregate version, event types and payload versions are
registered, unsupported versions fail explicitly, and replay orders by aggregate sequence rather
than by timestamp.

Only the first sequence number and the ordering rule live here. Uniqueness and atomic append are
database properties and are enforced there — a domain type cannot promise them, and claiming so in a
docstring would be the kind of overclaim `AI_CONTRACT.md` §10 forbids.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    event_type: str = Field(min_length=1)
    schema_version: int = Field(ge=1)
    #: When the fact happened, per the producer.
    occurred_at: datetime
    #: When the store accepted it. Distinct from `occurred_at`: a late append is not a late event.
    recorded_at: datetime
    correlation_id: UUID
    causation_id: UUID | None = None
    idempotency_key: str | None = None
    payload: tuple[tuple[str, str], ...] = ()

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

    def __init__(self, event_type: str, schema_version: int, supported: frozenset[int]) -> None:
        self.event_type = event_type
        self.schema_version = schema_version
        self.supported = supported
        known = ", ".join(str(v) for v in sorted(supported)) or "none"
        super().__init__(
            f"event `{event_type}` schema version {schema_version} is not supported "
            f"(known versions: {known})"
        )


def replay_order(events: tuple[EventEnvelope, ...]) -> tuple[EventEnvelope, ...]:
    """Order events for replay by aggregate and sequence, never by timestamp.

    Two events can share a timestamp, and clocks disagree across hosts. Sequence is the only total
    order within an aggregate.
    """
    return tuple(sorted(events, key=lambda event: (str(event.aggregate_id), event.sequence)))

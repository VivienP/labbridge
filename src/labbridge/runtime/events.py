"""Transactional append and version-checked loading for campaign event streams."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Final

from sqlalchemy import Connection, func, select, update

from labbridge.domain.events import (
    FIRST_SEQUENCE,
    EventEnvelope,
    validate_event_payload,
)
from labbridge.infrastructure.persistence.tables import campaigns, events

CURRENT_SCHEMA_VERSION: Final = 1
COMPLETE_STREAM_CONTRACT_VERSION: Final = 1


class ExpectedVersionError(RuntimeError):
    """The aggregate moved since the caller read it."""

    code: ClassVar[str] = "expected_version_mismatch"

    def __init__(self, aggregate_id: uuid.UUID, expected: int, actual: int) -> None:
        self.aggregate_id = aggregate_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"aggregate {aggregate_id} is at sequence {actual}, caller expected {expected}"
        )


class IncompleteEventStreamError(RuntimeError):
    """A campaign predates the complete event-stream contract."""

    code: ClassVar[str] = "incomplete_event_stream"

    def __init__(self, campaign_id: uuid.UUID, contract_version: int) -> None:
        self.campaign_id = campaign_id
        self.contract_version = contract_version
        super().__init__(
            f"campaign {campaign_id} has legacy/incomplete event stream contract version "
            f"{contract_version}"
        )


class CampaignPositionGapError(RuntimeError):
    """Campaign positions are missing or disagree with campaign metadata."""

    code: ClassVar[str] = "campaign_position_gap"


class AggregateSequenceGapError(RuntimeError):
    """An aggregate sequence does not start at one or contains a gap."""

    code: ClassVar[str] = "aggregate_sequence_gap"


class InvalidEventCausationError(RuntimeError):
    """An event cause is missing, later, cross-campaign, or cross-correlation."""

    code: ClassVar[str] = "invalid_event_causation"


class EventIdentityMismatchError(RuntimeError):
    """Payload identity fields disagree with the event envelope."""

    code: ClassVar[str] = "event_identity_mismatch"


class InvalidEventTimestampError(ValueError):
    """A producer supplied a timestamp without an unambiguous timezone."""

    code: ClassVar[str] = "invalid_event_timestamp"


@dataclass(frozen=True)
class AppendedEvent:
    event_id: uuid.UUID
    sequence: int
    campaign_position: int
    correlation_id: uuid.UUID


def current_sequence(
    connection: Connection,
    *,
    campaign_id: uuid.UUID,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
) -> int:
    """Return the current version of one aggregate within one campaign."""
    highest = connection.execute(
        select(func.max(events.c.sequence)).where(
            events.c.campaign_id == campaign_id,
            events.c.aggregate_type == aggregate_type,
            events.c.aggregate_id == aggregate_id,
        )
    ).scalar_one()
    return int(highest or 0)


def _lock_complete_campaign(connection: Connection, campaign_id: uuid.UUID) -> tuple[int, int]:
    row = connection.execute(
        select(
            campaigns.c.event_stream_contract_version,
            campaigns.c.event_stream_last_position,
        )
        .where(campaigns.c.campaign_id == campaign_id)
        .with_for_update()
    ).one()
    contract_version = int(row.event_stream_contract_version)
    if contract_version != COMPLETE_STREAM_CONTRACT_VERSION:
        raise IncompleteEventStreamError(campaign_id, contract_version)
    return contract_version, int(row.event_stream_last_position)


def _validate_event_identity(
    *,
    campaign_id: uuid.UUID,
    aggregate_id: uuid.UUID,
    aggregate_type: str,
    event_type: str,
    payload: dict[str, object],
) -> None:
    if aggregate_type == "campaign" and aggregate_id != campaign_id:
        raise EventIdentityMismatchError(
            f"campaign event `{event_type}` aggregate {aggregate_id} differs from campaign "
            f"{campaign_id}"
        )
    payload_campaign_id = payload.get("campaign_id")
    if payload_campaign_id is not None and uuid.UUID(str(payload_campaign_id)) != campaign_id:
        raise EventIdentityMismatchError(
            f"event `{event_type}` payload campaign {payload_campaign_id} differs from envelope "
            f"campaign {campaign_id}"
        )
    if event_type == "observation.accepted":
        payload_attempt_id = uuid.UUID(str(payload["attempt_id"]))
        if payload_attempt_id != aggregate_id:
            raise EventIdentityMismatchError(
                f"event `{event_type}` payload attempt {payload_attempt_id} differs from aggregate "
                f"{aggregate_id}"
            )


def _validate_append_causation(
    connection: Connection,
    *,
    campaign_id: uuid.UUID,
    correlation_id: uuid.UUID,
    causation_id: uuid.UUID | None,
    last_position: int,
) -> None:
    if causation_id is None:
        return
    cause = connection.execute(
        select(events.c.campaign_id, events.c.correlation_id, events.c.campaign_position).where(
            events.c.event_id == causation_id
        )
    ).one_or_none()
    if cause is None:
        raise InvalidEventCausationError(f"causal event {causation_id} does not exist")
    if cause.campaign_id != campaign_id:
        raise InvalidEventCausationError(
            f"causal event {causation_id} belongs to campaign {cause.campaign_id}, "
            f"not {campaign_id}"
        )
    if cause.correlation_id != correlation_id:
        raise InvalidEventCausationError(
            f"causal event {causation_id} has correlation {cause.correlation_id}, not "
            f"{correlation_id}"
        )
    if cause.campaign_position > last_position:
        raise InvalidEventCausationError(
            f"causal event {causation_id} is not earlier than the next campaign position"
        )


def append_event(
    connection: Connection,
    *,
    campaign_id: uuid.UUID,
    aggregate_id: uuid.UUID,
    aggregate_type: str,
    event_type: str,
    payload: object,
    expected_version: int,
    correlation_id: uuid.UUID,
    causation_id: uuid.UUID | None,
    idempotency_key: str | None = None,
    occurred_at: datetime | None = None,
) -> AppendedEvent:
    """Validate and append one event while holding the campaign sequencing lock."""
    if occurred_at is not None and (occurred_at.tzinfo is None or occurred_at.utcoffset() is None):
        raise InvalidEventTimestampError("occurred_at must be timezone-aware")
    _, last_position = _lock_complete_campaign(connection, campaign_id)
    actual = current_sequence(
        connection,
        campaign_id=campaign_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
    )
    if expected_version != actual:
        raise ExpectedVersionError(aggregate_id, expected_version, actual)
    if event_type == "campaign.created" and (actual != 0 or last_position != 0):
        raise InvalidEventCausationError(
            "campaign.created must be the first event in a complete campaign stream"
        )

    validated_payload = validate_event_payload(
        event_type=event_type,
        schema_version=CURRENT_SCHEMA_VERSION,
        aggregate_type=aggregate_type,
        causation_id=causation_id,
        payload=payload,
    )
    _validate_event_identity(
        campaign_id=campaign_id,
        aggregate_id=aggregate_id,
        aggregate_type=aggregate_type,
        event_type=event_type,
        payload=validated_payload,
    )
    _validate_append_causation(
        connection,
        campaign_id=campaign_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
        last_position=last_position,
    )
    appended = AppendedEvent(
        event_id=uuid.uuid4(),
        sequence=actual + FIRST_SEQUENCE,
        campaign_position=last_position + FIRST_SEQUENCE,
        correlation_id=correlation_id,
    )
    connection.execute(
        events.insert().values(
            event_id=appended.event_id,
            campaign_id=campaign_id,
            aggregate_id=aggregate_id,
            aggregate_type=aggregate_type,
            sequence=appended.sequence,
            campaign_position=appended.campaign_position,
            event_type=event_type,
            schema_version=CURRENT_SCHEMA_VERSION,
            occurred_at=occurred_at or func.now(),
            recorded_at=func.now(),
            correlation_id=correlation_id,
            causation_id=causation_id,
            idempotency_key=idempotency_key,
            payload=validated_payload,
        )
    )
    connection.execute(
        update(campaigns)
        .where(campaigns.c.campaign_id == campaign_id)
        .values(event_stream_last_position=appended.campaign_position)
    )
    return appended


def load_replay_stream(connection: Connection, campaign_id: uuid.UUID) -> tuple[EventEnvelope, ...]:
    """Load and validate a complete stream without reconstructing its projections."""
    contract_version, last_position = _lock_complete_campaign(connection, campaign_id)
    del contract_version
    rows = connection.execute(
        select(events)
        .where(events.c.campaign_id == campaign_id)
        .order_by(events.c.campaign_position)
    ).mappings()
    loaded: list[EventEnvelope] = []
    aggregate_versions: dict[tuple[str, uuid.UUID], int] = {}
    prior_events: dict[uuid.UUID, uuid.UUID] = {}
    for expected_position, row in enumerate(rows, start=FIRST_SEQUENCE):
        if row["campaign_position"] != expected_position:
            raise CampaignPositionGapError(
                f"campaign {campaign_id} expected position {expected_position}, "
                f"found {row['campaign_position']}"
            )
        aggregate_key = (row["aggregate_type"], row["aggregate_id"])
        expected_sequence = aggregate_versions.get(aggregate_key, 0) + FIRST_SEQUENCE
        if row["sequence"] != expected_sequence:
            raise AggregateSequenceGapError(
                f"campaign {campaign_id} aggregate {row['aggregate_type']}/"
                f"{row['aggregate_id']} expected sequence {expected_sequence}, "
                f"found {row['sequence']}"
            )
        if row["event_type"] == "campaign.created" and (
            expected_position != FIRST_SEQUENCE or row["sequence"] != FIRST_SEQUENCE
        ):
            raise InvalidEventCausationError(
                "campaign.created must be the first event in a complete campaign stream"
            )
        payload = validate_event_payload(
            event_type=row["event_type"],
            schema_version=row["schema_version"],
            aggregate_type=row["aggregate_type"],
            causation_id=row["causation_id"],
            payload=row["payload"],
        )
        _validate_event_identity(
            campaign_id=campaign_id,
            aggregate_id=row["aggregate_id"],
            aggregate_type=row["aggregate_type"],
            event_type=row["event_type"],
            payload=payload,
        )
        causation_id = row["causation_id"]
        if causation_id is not None:
            cause_correlation_id = prior_events.get(causation_id)
            if cause_correlation_id is None:
                raise InvalidEventCausationError(
                    f"event {row['event_id']} refers to absent or non-prior cause {causation_id}"
                )
            if cause_correlation_id != row["correlation_id"]:
                raise InvalidEventCausationError(
                    f"event {row['event_id']} and cause {causation_id} have different correlations"
                )
        loaded.append(EventEnvelope.model_validate({**row, "payload": payload}))
        aggregate_versions[aggregate_key] = row["sequence"]
        prior_events[row["event_id"]] = row["correlation_id"]
    if not loaded:
        raise InvalidEventCausationError(
            f"complete campaign {campaign_id} has no campaign.created root event"
        )
    if len(loaded) != last_position:
        raise CampaignPositionGapError(
            f"campaign {campaign_id} metadata ends at {last_position}, loaded {len(loaded)} events"
        )
    return tuple(loaded)


def read_stream(connection: Connection, campaign_id: uuid.UUID) -> list[dict[str, object]]:
    """Read raw event rows for evidence export, including legacy streams."""
    contract_version = connection.execute(
        select(campaigns.c.event_stream_contract_version).where(
            campaigns.c.campaign_id == campaign_id
        )
    ).scalar_one()
    if contract_version == COMPLETE_STREAM_CONTRACT_VERSION:
        return [
            event.model_dump(mode="json") for event in load_replay_stream(connection, campaign_id)
        ]
    rows = connection.execute(
        select(
            events.c.event_id,
            events.c.aggregate_id,
            events.c.aggregate_type,
            events.c.sequence,
            events.c.campaign_position,
            events.c.event_type,
            events.c.schema_version,
            events.c.occurred_at,
            events.c.recorded_at,
            events.c.correlation_id,
            events.c.causation_id,
            events.c.idempotency_key,
            events.c.payload,
        )
        .where(events.c.campaign_id == campaign_id)
        .order_by(events.c.campaign_position)
    ).mappings()
    return [dict(row) for row in rows]

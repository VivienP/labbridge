"""Appending to the event log.

`docs/SPEC.md` §5.1: `sequence` is unique and monotonic per aggregate, and an append uses an
expected aggregate version. Both are enforced by the unique constraint on
`(aggregate_id, sequence)`, not by this code — under concurrency two appenders can read the same
"next" sequence, and only the database can decide which one wins.

That is why `append_event` does not lock or retry: the loser gets an `IntegrityError`, which is the
correct signal that its view of the aggregate was stale. Swallowing it and retrying with the next
free number would append an event computed against a state that no longer holds.

Replay orders by `(aggregate_id, sequence)` and never by timestamp. Two events can share a
timestamp, and clocks disagree across hosts; the sequence is the only total order within an
aggregate.
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy import Connection, func, select

from labbridge.domain.events import FIRST_SEQUENCE
from labbridge.infrastructure.persistence.tables import events

#: Every event payload written by this build. Bumped per event type when its shape changes, so an
#: unknown version fails explicitly rather than being read with the closest known schema.
CURRENT_SCHEMA_VERSION: Final = 1


class ExpectedVersionError(RuntimeError):
    """The aggregate moved since the caller read it.

    Raised rather than resolved: an append computed against a stale state is not made correct by
    giving it a later sequence number.
    """

    def __init__(self, aggregate_id: uuid.UUID, expected: int, actual: int) -> None:
        self.aggregate_id = aggregate_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"aggregate {aggregate_id} is at sequence {actual}, caller expected {expected}"
        )


def current_sequence(connection: Connection, aggregate_id: uuid.UUID) -> int:
    """The highest sequence recorded for this aggregate, or 0 when it has no events yet."""
    highest = connection.execute(
        select(func.max(events.c.sequence)).where(events.c.aggregate_id == aggregate_id)
    ).scalar_one()
    return int(highest or 0)


def append_event(
    connection: Connection,
    *,
    campaign_id: uuid.UUID,
    aggregate_id: uuid.UUID,
    aggregate_type: str,
    event_type: str,
    payload: dict[str, str],
    expected_version: int | None = None,
    correlation_id: uuid.UUID | None = None,
    causation_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
) -> int:
    """Append one event and return its sequence.

    `expected_version` is optional because most appends in Slice 1 happen inside a transaction that
    already holds the aggregate's row. When it is supplied, a mismatch raises instead of writing.
    """
    actual = current_sequence(connection, aggregate_id)
    if expected_version is not None and expected_version != actual:
        raise ExpectedVersionError(aggregate_id, expected_version, actual)

    sequence = actual + 1 if actual else FIRST_SEQUENCE
    connection.execute(
        events.insert().values(
            event_id=uuid.uuid4(),
            campaign_id=campaign_id,
            aggregate_id=aggregate_id,
            aggregate_type=aggregate_type,
            sequence=sequence,
            event_type=event_type,
            schema_version=CURRENT_SCHEMA_VERSION,
            occurred_at=func.now(),
            recorded_at=func.now(),
            correlation_id=correlation_id or uuid.uuid4(),
            causation_id=causation_id,
            idempotency_key=idempotency_key,
            payload=payload,
        )
    )
    return sequence


def read_stream(connection: Connection, campaign_id: uuid.UUID) -> list[dict[str, object]]:
    """Every event of a campaign, ordered for replay.

    Ordered by aggregate then sequence, which is the order `docs/SPEC.md` §5.1 requires and which a
    timestamp ordering would silently get wrong when two events share a moment.
    """
    rows = connection.execute(
        select(
            events.c.aggregate_id,
            events.c.aggregate_type,
            events.c.sequence,
            events.c.event_type,
            events.c.schema_version,
            events.c.payload,
        )
        .where(events.c.campaign_id == campaign_id)
        .order_by(events.c.aggregate_id, events.c.sequence)
    ).all()
    return [
        {
            "aggregate_id": str(row.aggregate_id),
            "aggregate_type": row.aggregate_type,
            "sequence": row.sequence,
            "event_type": row.event_type,
            "schema_version": row.schema_version,
            "payload": row.payload,
        }
        for row in rows
    ]

"""The database refuses what the domain refuses.

A rule enforced only in Python holds until the next writer — a repair script, a migration, an admin
session. These tests prove PostgreSQL rejects the same shapes, which is the difference between a
convention and an invariant (`AI_CONTRACT.md` invariant 1, §9).
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Connection
from sqlalchemy.exc import IntegrityError

from labbridge.domain.identity import ADMISSIBLE_PAIRS
from labbridge.infrastructure.persistence.tables import (
    attempt_outcomes,
    attempts,
    campaigns,
    events,
    observations,
    storage_objects,
    work_items,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
SHA = "c" * 64
ORIGINS = ("observed", "synthetic")
MODES = ("replay", "simulation", "live")
TWO_RECEIPTS = 2


@contextmanager
def expect_violation(connection: Connection, match: str) -> Iterator[None]:
    """Assert a constraint fires, inside a savepoint.

    PostgreSQL aborts the whole transaction on an integrity error, so without the savepoint the
    first expected failure would poison every later statement in the same test.
    """
    savepoint = connection.begin_nested()
    try:
        with pytest.raises(IntegrityError, match=match):
            yield
    finally:
        savepoint.rollback()


def _campaign(connection: Connection, **overrides: Any) -> uuid.UUID:
    campaign_id = uuid.uuid4()
    values: dict[str, Any] = {
        "campaign_id": campaign_id,
        "name": "fixture campaign",
        "environment_id": "her",
        "adapter_version": "1",
        "data_origin": "synthetic",
        "execution_mode": "replay",
        "state": "draft",
        "declaration": {},
        "declaration_hash": SHA,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    connection.execute(campaigns.insert().values(**values))
    return campaign_id


def _work_item(connection: Connection, campaign_id: uuid.UUID, **overrides: Any) -> uuid.UUID:
    work_item_id = uuid.uuid4()
    values: dict[str, Any] = {
        "work_item_id": work_item_id,
        "campaign_id": campaign_id,
        "candidate_id": f"cand:{uuid.uuid4().hex}",
        "candidate": {},
        "state": "queued",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    connection.execute(work_items.insert().values(**values))
    return work_item_id


def _attempt(connection: Connection, work_item_id: uuid.UUID, ordinal: int) -> uuid.UUID:
    attempt_id = uuid.uuid4()
    connection.execute(
        attempts.insert().values(
            attempt_id=attempt_id,
            work_item_id=work_item_id,
            ordinal=ordinal,
            state="running",
            created_at=NOW,
        )
    )
    return attempt_id


def _outcome(
    connection: Connection,
    campaign_id: uuid.UUID,
    work_item_id: uuid.UUID,
    attempt_id: uuid.UUID,
    status: str,
    **overrides: Any,
) -> None:
    values: dict[str, Any] = {
        "attempt_id": attempt_id,
        "work_item_id": work_item_id,
        "campaign_id": campaign_id,
        "status": status,
        "data_origin": "synthetic",
        "execution_mode": "replay",
        "provenance": {},
        "finished_at": NOW,
    }
    values.update(overrides)
    connection.execute(attempt_outcomes.insert().values(**values))


@pytest.mark.parametrize(("origin", "mode"), list(itertools.product(ORIGINS, MODES)))
def test_the_database_admits_exactly_the_pairs_the_domain_admits(
    connection: Connection, origin: str, mode: str
) -> None:
    """The pair list is deliberately duplicated in SQL so a writer that is not this application
    cannot bypass it. Duplication is only safe while a test proves the two copies agree, and the
    whole product is enumerated so a widening on either side fails here."""
    if (origin, mode) in ADMISSIBLE_PAIRS:
        assert _campaign(connection, data_origin=origin, execution_mode=mode)
        return
    with expect_violation(connection, "admissible_origin_mode"):
        _campaign(connection, data_origin=origin, execution_mode=mode)


def test_observed_simulation_is_the_pair_the_database_must_never_hold(
    connection: Connection,
) -> None:
    """Named on its own because it is the conflation invariant 1 exists to prevent."""
    with expect_violation(connection, "admissible_origin_mode"):
        _campaign(connection, data_origin="observed", execution_mode="simulation")


def test_only_one_succeeded_outcome_per_work_item(connection: Connection) -> None:
    """PO-02. A duplicate delivery must not become a second accepted result."""
    campaign_id = _campaign(connection)
    work_item_id = _work_item(connection, campaign_id)
    first = _attempt(connection, work_item_id, 1)
    second = _attempt(connection, work_item_id, 2)

    _outcome(connection, campaign_id, work_item_id, first, "succeeded")

    with expect_violation(connection, "one_success_per_work_item"):
        _outcome(connection, campaign_id, work_item_id, second, "succeeded")


def test_a_failed_attempt_may_be_retried_alongside_a_later_success(
    connection: Connection,
) -> None:
    """The index is partial on purpose: only successes are unique, so retries stay recordable."""
    campaign_id = _campaign(connection)
    work_item_id = _work_item(connection, campaign_id)
    first = _attempt(connection, work_item_id, 1)
    second = _attempt(connection, work_item_id, 2)

    _outcome(
        connection,
        campaign_id,
        work_item_id,
        first,
        "failed_retryable",
        failure={"failure_code": "timeout", "retryable": True},
    )
    _outcome(connection, campaign_id, work_item_id, second, "succeeded")


def test_an_outcome_with_no_bytes_cannot_reference_an_observation(
    connection: Connection,
) -> None:
    campaign_id = _campaign(connection)
    work_item_id = _work_item(connection, campaign_id)
    attempt_id = _attempt(connection, work_item_id, 1)
    observation_id = _observation(connection, campaign_id, work_item_id, attempt_id)

    with expect_violation(connection, "observation_only_when_bytes_arrived"):
        _outcome(
            connection,
            campaign_id,
            work_item_id,
            attempt_id,
            "timed_out",
            observation_id=observation_id,
        )


def _observation(
    connection: Connection,
    campaign_id: uuid.UUID,
    work_item_id: uuid.UUID,
    attempt_id: uuid.UUID,
    **overrides: Any,
) -> str:
    observation_id = f"obs:{uuid.uuid4().hex}"
    values: dict[str, Any] = {
        "observation_id": observation_id,
        "campaign_id": campaign_id,
        "work_item_id": work_item_id,
        "attempt_id": attempt_id,
        "media_type": "text/csv",
        "object_uri": f"s3://labbridge/{observation_id}",
        "byte_size": 10,
        "sha256": SHA,
        "schema_version": "1",
        "signal_kind": "lsv",
        "quantities": [],
        "status": "received",
        # `received` means bytes arrived but were not accepted, and the schema requires that a
        # retained receipt says why — the same rule `corrupted` has always carried.
        "status_reason": "retained for constraint tests",
        "data_origin": "synthetic",
        "execution_mode": "replay",
        "provenance": {},
        "received_at": NOW,
    }
    values.update(overrides)
    connection.execute(observations.insert().values(**values))
    return observation_id


def test_a_corrupted_observation_is_storable_and_must_state_why(connection: Connection) -> None:
    """ADR-005 and PO-05: bytes that arrived are kept, with the reason they were rejected."""
    campaign_id = _campaign(connection)
    work_item_id = _work_item(connection, campaign_id)
    attempt_id = _attempt(connection, work_item_id, 1)

    assert _observation(
        connection,
        campaign_id,
        work_item_id,
        attempt_id,
        status="corrupted",
        status_reason="array length mismatch",
    )

    with expect_violation(connection, "rejected_status_states_its_reason"):
        _observation(
            connection,
            campaign_id,
            work_item_id,
            _attempt(connection, work_item_id, 2),
            status="corrupted",
            status_reason=None,
        )


def test_an_event_sequence_is_unique_per_aggregate(connection: Connection) -> None:
    """§5.1. This is what makes an expected-version append safe: a racing writer gets an error."""
    campaign_id = _campaign(connection)
    aggregate_id = uuid.uuid4()

    def append(sequence: int, position: int, aggregate: uuid.UUID = aggregate_id) -> None:
        connection.execute(
            events.insert().values(
                event_id=uuid.uuid4(),
                campaign_id=campaign_id,
                aggregate_id=aggregate,
                aggregate_type="campaign",
                sequence=sequence,
                campaign_position=position,
                event_type="campaign.declared",
                schema_version=1,
                occurred_at=NOW,
                recorded_at=NOW,
                correlation_id=uuid.uuid4(),
                payload={},
            )
        )

    append(1, 1)
    append(2, 2)
    with expect_violation(connection, "uq_events_aggregate_sequence"):
        append(2, 3)

    with expect_violation(connection, "uq_events_campaign_position"):
        append(3, 2)

    # A different aggregate has its own sequence space.
    append(1, 3, uuid.uuid4())


def test_an_object_cannot_be_committed_without_a_verified_checksum(
    connection: Connection,
) -> None:
    """§4.2: a record must not declare an artifact committed before its checksum is verified."""
    with expect_violation(connection, "committed_object_is_verified"):
        connection.execute(
            storage_objects.insert().values(
                object_uri="s3://labbridge/pending",
                bucket="labbridge",
                object_key="pending",
                state="committed",
                created_at=NOW,
            )
        )


def test_a_pending_object_needs_no_checksum_yet(connection: Connection) -> None:
    connection.execute(
        storage_objects.insert().values(
            object_uri="s3://labbridge/staging",
            bucket="labbridge",
            object_key="staging",
            state="pending",
            created_at=NOW,
        )
    )


def test_a_quarantined_work_item_must_record_why(connection: Connection) -> None:
    campaign_id = _campaign(connection)

    with expect_violation(connection, "quarantine_states_its_reason"):
        _work_item(connection, campaign_id, state="quarantined")

    assert _work_item(
        connection, campaign_id, state="quarantined", quarantine_reason="repeated collisions"
    )


def test_one_attempt_ordinal_per_work_item(connection: Connection) -> None:
    """A retry is a new ordinal, so two attempts cannot silently claim to be the same try."""
    campaign_id = _campaign(connection)
    work_item_id = _work_item(connection, campaign_id)
    _attempt(connection, work_item_id, 1)

    with expect_violation(connection, "uq_attempts_work_item_ordinal"):
        _attempt(connection, work_item_id, 1)


def test_the_same_content_received_by_two_attempts_is_two_rows(connection: Connection) -> None:
    """`observation_id` is content-derived, so two campaigns replaying one fixture location produce
    identical content. A single-column primary key rejected the second receipt — losing bytes
    invariant 2 requires retaining — or attributed it to the first campaign's attempt."""
    first_campaign = _campaign(connection)
    second_campaign = _campaign(connection)
    first_attempt = _attempt(connection, _work_item(connection, first_campaign), 1)
    second_item = _work_item(connection, second_campaign)
    second_attempt = _attempt(connection, second_item, 1)

    shared = f"obs:{uuid.uuid4().hex}"
    _observation(
        connection,
        first_campaign,
        _work_item(connection, first_campaign),
        first_attempt,
        observation_id=shared,
    )
    _observation(connection, second_campaign, second_item, second_attempt, observation_id=shared)

    rows = connection.execute(
        observations.select().where(observations.c.observation_id == shared)
    ).fetchall()
    assert len(rows) == TWO_RECEIPTS


def test_an_observation_may_not_contradict_its_campaigns_origin(connection: Connection) -> None:
    """The conflation ADR-010 exists to prevent happens *between* rows: both pairs are admissible
    on their own, so a per-row CHECK never sees it."""
    campaign_id = _campaign(connection, data_origin="observed", execution_mode="replay")
    work_item_id = _work_item(connection, campaign_id)
    attempt_id = _attempt(connection, work_item_id, 1)

    with expect_violation(connection, "fk_observations_campaign_identity"):
        _observation(
            connection,
            campaign_id,
            work_item_id,
            attempt_id,
            data_origin="synthetic",
            execution_mode="replay",
        )


def test_an_outcome_may_not_contradict_its_campaigns_origin(connection: Connection) -> None:
    campaign_id = _campaign(connection, data_origin="observed", execution_mode="replay")
    work_item_id = _work_item(connection, campaign_id)
    attempt_id = _attempt(connection, work_item_id, 1)

    with expect_violation(connection, "fk_attempt_outcomes_campaign_identity"):
        _outcome(
            connection,
            campaign_id,
            work_item_id,
            attempt_id,
            "succeeded",
            data_origin="synthetic",
            execution_mode="replay",
        )


def test_an_outcome_carries_its_own_provenance(connection: Connection) -> None:
    """An outcome with no observation — timed_out, lease_lost — would otherwise leave the lineage
    root and code version recoverable from no row at all."""
    campaign_id = _campaign(connection)
    work_item_id = _work_item(connection, campaign_id)
    attempt_id = _attempt(connection, work_item_id, 1)

    _outcome(
        connection,
        campaign_id,
        work_item_id,
        attempt_id,
        "timed_out",
        provenance={"code_version": "1", "environment": {"data_origin": "synthetic"}},
    )

    stored = connection.execute(
        attempt_outcomes.select().where(attempt_outcomes.c.attempt_id == attempt_id)
    ).one()
    assert stored.provenance["code_version"] == "1"
    assert stored.data_origin == "synthetic"


@pytest.mark.parametrize("bogus", ["Succeeded", "succeeded ", "SUCCEEDED"])
def test_a_status_outside_the_known_set_is_refused(connection: Connection, bogus: str) -> None:
    """The PO-02 index keys on the literal `status = 'succeeded'`, so a near-miss spelling would
    record a second success the index never consults."""
    campaign_id = _campaign(connection)
    work_item_id = _work_item(connection, campaign_id)
    attempt_id = _attempt(connection, work_item_id, 1)

    with expect_violation(connection, "ck_attempt_outcomes_known_status"):
        _outcome(connection, campaign_id, work_item_id, attempt_id, bogus)

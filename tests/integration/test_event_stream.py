from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import Connection, Engine, func, select, update

from labbridge.evidence.bundle import build_bundle
from labbridge.infrastructure.persistence.tables import campaigns, events
from labbridge.runtime.events import (
    AggregateSequenceGapError,
    CampaignPositionGapError,
    EventIdentityMismatchError,
    ExpectedVersionError,
    IncompleteEventStreamError,
    InvalidEventCausationError,
    InvalidEventTimestampError,
    append_event,
    load_replay_stream,
)

pytestmark = pytest.mark.integration


def _campaign(connection: Connection, *, contract_version: int) -> uuid.UUID:
    campaign_id = uuid.uuid4()
    connection.execute(
        campaigns.insert().values(
            campaign_id=campaign_id,
            name="event stream test",
            environment_id="her",
            adapter_version="1",
            data_origin="synthetic",
            execution_mode="simulation",
            state="active",
            declaration={},
            declaration_hash="a" * 64,
            event_stream_contract_version=contract_version,
            event_stream_last_position=0,
            created_at=func.now(),
            updated_at=func.now(),
        )
    )
    return campaign_id


def _created_payload() -> dict[str, object]:
    return {
        "name": "event stream test",
        "environment_id": "her",
        "adapter_version": "1",
        "data_origin": "synthetic",
        "execution_mode": "simulation",
        "declaration": {},
        "declaration_hash": "a" * 64,
        "state": "active",
    }


def _queued_payload(candidate_id: str) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "candidate": {
            "kind": "her_location",
            "library_id": "library",
            "measurement_area_id": "area",
            "grid_x": {"value": "1", "unit": "mm"},
            "grid_y": {"value": "2", "unit": "mm"},
        },
        "state": "queued",
    }


def _append_root(connection: Connection, campaign_id: uuid.UUID):
    return append_event(
        connection,
        campaign_id=campaign_id,
        aggregate_id=campaign_id,
        aggregate_type="campaign",
        event_type="campaign.created",
        payload=_created_payload(),
        expected_version=0,
        correlation_id=uuid.uuid4(),
        causation_id=None,
    )


def test_a_legacy_campaign_is_refused_by_the_replay_loader(connection: Connection) -> None:
    campaign_id = _campaign(connection, contract_version=0)

    with pytest.raises(IncompleteEventStreamError) as caught:
        load_replay_stream(connection, campaign_id)

    assert caught.value.code == "incomplete_event_stream"
    assert caught.value.contract_version == 0


def test_a_complete_campaign_without_its_root_event_is_refused(connection: Connection) -> None:
    campaign_id = _campaign(connection, contract_version=1)

    with pytest.raises(InvalidEventCausationError):
        load_replay_stream(connection, campaign_id)


def test_append_rejects_a_naive_producer_timestamp(connection: Connection) -> None:
    campaign_id = _campaign(connection, contract_version=1)

    with pytest.raises(InvalidEventTimestampError):
        append_event(
            connection,
            campaign_id=campaign_id,
            aggregate_id=campaign_id,
            aggregate_type="campaign",
            event_type="campaign.created",
            payload=_created_payload(),
            expected_version=0,
            correlation_id=uuid.uuid4(),
            causation_id=None,
            occurred_at=datetime(2026, 8, 1),
        )


def test_a_legacy_campaign_bundle_is_labelled_incomplete(
    connection: Connection, tmp_path: Path
) -> None:
    campaign_id = _campaign(connection, contract_version=0)

    manifest = build_bundle(
        connection,
        campaign_id,
        tmp_path / "legacy-bundle",
        generated_at=datetime(2026, 8, 1, tzinfo=UTC),
    )

    assert manifest["event_stream_contract_version"] == 0
    assert manifest["event_stream_completeness"] == "legacy_incomplete"


def test_append_requires_the_exact_aggregate_version_and_assigns_campaign_positions(
    connection: Connection,
) -> None:
    campaign_id = _campaign(connection, contract_version=1)
    root = _append_root(connection, campaign_id)
    work_item_id = uuid.uuid4()
    child = append_event(
        connection,
        campaign_id=campaign_id,
        aggregate_id=work_item_id,
        aggregate_type="work_item",
        event_type="work_item.queued",
        payload=_queued_payload("cand:test"),
        expected_version=0,
        correlation_id=root.correlation_id,
        causation_id=root.event_id,
    )

    assert (root.sequence, root.campaign_position) == (1, 1)
    assert (child.sequence, child.campaign_position) == (1, 2)
    assert child.correlation_id == root.correlation_id

    stream = load_replay_stream(connection, campaign_id)
    assert [event.event_id for event in stream] == [root.event_id, child.event_id]


def test_a_stale_expected_version_writes_nothing(connection: Connection) -> None:
    campaign_id = _campaign(connection, contract_version=1)
    _append_root(connection, campaign_id)

    with pytest.raises(ExpectedVersionError):
        append_event(
            connection,
            campaign_id=campaign_id,
            aggregate_id=campaign_id,
            aggregate_type="campaign",
            event_type="campaign.started",
            payload={"state": "active", "reason": None},
            expected_version=0,
            correlation_id=uuid.uuid4(),
            causation_id=uuid.uuid4(),
        )

    assert (
        connection.execute(
            select(func.count()).select_from(events).where(events.c.campaign_id == campaign_id)
        ).scalar_one()
        == 1
    )


def test_concurrent_appends_with_one_expected_version_have_one_winner(
    migrated: Engine,
) -> None:
    with migrated.begin() as connection:
        campaign_id = _campaign(connection, contract_version=1)
    correlation_id = uuid.uuid4()

    def write() -> str:
        try:
            with migrated.begin() as connection:
                append_event(
                    connection,
                    campaign_id=campaign_id,
                    aggregate_id=campaign_id,
                    aggregate_type="campaign",
                    event_type="campaign.created",
                    payload=_created_payload(),
                    expected_version=0,
                    correlation_id=correlation_id,
                    causation_id=None,
                )
            return "written"
        except ExpectedVersionError:
            return "stale"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: write(), range(2)))

        assert sorted(outcomes) == ["stale", "written"]
        with migrated.connect() as connection:
            assert (
                connection.execute(
                    select(func.count())
                    .select_from(events)
                    .where(events.c.campaign_id == campaign_id)
                ).scalar_one()
                == 1
            )
    finally:
        with migrated.begin() as connection:
            connection.execute(events.delete().where(events.c.campaign_id == campaign_id))
            connection.execute(campaigns.delete().where(campaigns.c.campaign_id == campaign_id))


def test_concurrent_appends_to_distinct_aggregates_get_contiguous_campaign_positions(
    migrated: Engine,
) -> None:
    with migrated.begin() as connection:
        campaign_id = _campaign(connection, contract_version=1)
        root = _append_root(connection, campaign_id)
    barrier = Barrier(2)

    def write(index: int) -> tuple[uuid.UUID, int]:
        aggregate_id = uuid.uuid4()
        barrier.wait()
        with migrated.begin() as connection:
            appended = append_event(
                connection,
                campaign_id=campaign_id,
                aggregate_id=aggregate_id,
                aggregate_type="work_item",
                event_type="work_item.queued",
                payload=_queued_payload(f"cand:{index}"),
                expected_version=0,
                correlation_id=root.correlation_id,
                causation_id=root.event_id,
            )
        return appended.event_id, appended.campaign_position

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write, index) for index in range(2)]
            results = [future.result(timeout=10) for future in futures]

        assert sorted(position for _, position in results) == [2, 3]
        with migrated.connect() as connection:
            stream = load_replay_stream(connection, campaign_id)
        assert [event.campaign_position for event in stream] == [1, 2, 3]
    finally:
        with migrated.begin() as connection:
            connection.execute(events.delete().where(events.c.campaign_id == campaign_id))
            connection.execute(campaigns.delete().where(campaigns.c.campaign_id == campaign_id))


def test_append_rejects_a_missing_or_cross_correlation_cause(connection: Connection) -> None:
    campaign_id = _campaign(connection, contract_version=1)
    root = _append_root(connection, campaign_id)

    with pytest.raises(InvalidEventCausationError):
        append_event(
            connection,
            campaign_id=campaign_id,
            aggregate_id=campaign_id,
            aggregate_type="campaign",
            event_type="campaign.started",
            payload={"state": "active", "reason": None},
            expected_version=1,
            correlation_id=root.correlation_id,
            causation_id=uuid.uuid4(),
        )
    with pytest.raises(InvalidEventCausationError):
        append_event(
            connection,
            campaign_id=campaign_id,
            aggregate_id=campaign_id,
            aggregate_type="campaign",
            event_type="campaign.started",
            payload={"state": "active", "reason": None},
            expected_version=1,
            correlation_id=uuid.uuid4(),
            causation_id=root.event_id,
        )


def test_append_rejects_a_cause_from_another_campaign(connection: Connection) -> None:
    first_campaign_id = _campaign(connection, contract_version=1)
    first_root = _append_root(connection, first_campaign_id)
    second_campaign_id = _campaign(connection, contract_version=1)
    _append_root(connection, second_campaign_id)

    with pytest.raises(InvalidEventCausationError):
        append_event(
            connection,
            campaign_id=second_campaign_id,
            aggregate_id=second_campaign_id,
            aggregate_type="campaign",
            event_type="campaign.started",
            payload={"state": "active", "reason": None},
            expected_version=1,
            correlation_id=first_root.correlation_id,
            causation_id=first_root.event_id,
        )


def test_a_second_campaign_root_is_refused(connection: Connection) -> None:
    campaign_id = _campaign(connection, contract_version=1)
    _append_root(connection, campaign_id)

    with pytest.raises(InvalidEventCausationError):
        append_event(
            connection,
            campaign_id=campaign_id,
            aggregate_id=campaign_id,
            aggregate_type="campaign",
            event_type="campaign.created",
            payload=_created_payload(),
            expected_version=1,
            correlation_id=uuid.uuid4(),
            causation_id=None,
        )


def test_loader_rejects_an_aggregate_sequence_gap(connection: Connection) -> None:
    campaign_id = _campaign(connection, contract_version=1)
    root = _append_root(connection, campaign_id)
    connection.execute(update(events).where(events.c.event_id == root.event_id).values(sequence=2))

    with pytest.raises(AggregateSequenceGapError):
        load_replay_stream(connection, campaign_id)


def test_loader_rejects_a_campaign_position_gap(connection: Connection) -> None:
    campaign_id = _campaign(connection, contract_version=1)
    root = _append_root(connection, campaign_id)
    connection.execute(
        update(events).where(events.c.event_id == root.event_id).values(campaign_position=2)
    )

    with pytest.raises(CampaignPositionGapError):
        load_replay_stream(connection, campaign_id)


def test_loader_rejects_a_non_prior_cause(connection: Connection) -> None:
    campaign_id = _campaign(connection, contract_version=1)
    root = _append_root(connection, campaign_id)
    child = append_event(
        connection,
        campaign_id=campaign_id,
        aggregate_id=uuid.uuid4(),
        aggregate_type="work_item",
        event_type="work_item.queued",
        payload=_queued_payload("cand:test"),
        expected_version=0,
        correlation_id=root.correlation_id,
        causation_id=root.event_id,
    )
    connection.execute(
        update(events).where(events.c.event_id == child.event_id).values(causation_id=uuid.uuid4())
    )

    with pytest.raises(InvalidEventCausationError):
        load_replay_stream(connection, campaign_id)


def test_append_rejects_payload_identity_that_differs_from_the_envelope(
    connection: Connection,
) -> None:
    campaign_id = _campaign(connection, contract_version=1)
    root = _append_root(connection, campaign_id)

    with pytest.raises(EventIdentityMismatchError):
        append_event(
            connection,
            campaign_id=campaign_id,
            aggregate_id=uuid.uuid4(),
            aggregate_type="attempt",
            event_type="attempt.completed",
            payload={
                "work_item_id": uuid.uuid4(),
                "campaign_id": uuid.uuid4(),
                "state": "failed_terminal",
                "status": "failed_terminal",
                "observation_id": None,
                "failure": {
                    "failure_code": "worker_failed",
                    "category": "worker",
                    "retryable": False,
                    "summary": "worker failed",
                },
                "cost": {},
                "data_origin": "synthetic",
                "execution_mode": "replay",
                "provenance": {
                    "environment": {
                        "environment_id": "her",
                        "adapter_version": "1",
                        "data_origin": "synthetic",
                        "execution_mode": "replay",
                    },
                    "code_version": "1",
                    "config_hash": "config",
                },
                "started_at": None,
                "finished_at": "2026-08-01T00:00:00Z",
            },
            expected_version=0,
            correlation_id=root.correlation_id,
            causation_id=root.event_id,
        )

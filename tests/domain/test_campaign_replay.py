from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from labbridge.domain.events import EventEnvelope
from labbridge.runtime import replay as replay_module
from labbridge.runtime.events import CampaignPositionGapError, validate_exported_stream

SemanticIncompleteEventStreamError = getattr(
    replay_module, "SemanticIncompleteEventStreamError", RuntimeError
)
reconstruct_campaign = getattr(
    replay_module, "reconstruct_campaign", lambda *_args, **_kwargs: None
)

RESERVED_TOTAL = 3
CONSUMED_TOTAL = 2


CAMPAIGN_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
WORK_ITEM_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
JOB_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
ATTEMPT_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")
RESERVATION_ID = uuid.UUID("50000000-0000-0000-0000-000000000001")
SETTLEMENT_ID = uuid.UUID("50000000-0000-0000-0000-000000000002")
ADJUSTMENT_ID = uuid.UUID("50000000-0000-0000-0000-000000000003")
CORRELATION_ID = uuid.UUID("60000000-0000-0000-0000-000000000001")
BASE_TIME = datetime(2026, 8, 13, 8, tzinfo=UTC)
OBSERVATION_ID = "obs:" + "a" * 64


def _provenance() -> dict[str, object]:
    return {
        "environment": {
            "environment_id": "her",
            "adapter_version": "1",
            "data_origin": "synthetic",
            "execution_mode": "replay",
        },
        "synthetic_root": {
            "generator": "fixture",
            "generator_version": "1",
            "seed": 7,
            "config_hash": "fixture-config",
        },
        "code_version": "1",
        "config_hash": "runtime-config",
    }


def _event(
    position: int,
    event_type: str,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    sequence: int,
    payload: dict[str, object],
) -> EventEnvelope:
    event_id = uuid.UUID(int=position)
    return EventEnvelope(
        event_id=event_id,
        campaign_id=CAMPAIGN_ID,
        aggregate_id=aggregate_id,
        aggregate_type=aggregate_type,
        sequence=sequence,
        campaign_position=position,
        event_type=event_type,
        schema_version=1,
        occurred_at=BASE_TIME + timedelta(seconds=position),
        recorded_at=BASE_TIME + timedelta(seconds=position),
        correlation_id=CORRELATION_ID,
        causation_id=None if position == 1 else uuid.UUID(int=position - 1),
        payload=payload,
    )


def _stream() -> tuple[EventEnvelope, ...]:
    candidate = {
        "kind": "her_location",
        "library_id": "library",
        "measurement_area_id": "area",
        "grid_x": {"value": "1", "unit": "mm"},
        "grid_y": {"value": "2", "unit": "mm"},
    }
    job_payload = {
        "work_item_id": WORK_ITEM_ID,
        "state": "succeeded",
        "available_at": BASE_TIME,
        "lease_owner": None,
        "lease_token": None,
        "lease_expires_at": None,
        "heartbeat_at": BASE_TIME,
        "attempt_count": 1,
        "lease_generation": 1,
        "max_attempts": 3,
        "command_version": "1",
        "idempotency_key": "instruction:one",
        "last_failure": None,
        "created_at": BASE_TIME,
        "updated_at": BASE_TIME + timedelta(seconds=12),
    }
    observation = {
        "observation_id": OBSERVATION_ID,
        "work_item_id": WORK_ITEM_ID,
        "attempt_id": ATTEMPT_ID,
        "media_type": "text/csv",
        "object_uri": "s3://labbridge/receipt.csv",
        "byte_size": 12,
        "sha256": "a" * 64,
        "schema_version": "1",
        "signal_kind": "lsv",
        "quantities": [],
        "status": "received",
        "status_reason": "retained after acceptance refusal",
        "data_origin": "synthetic",
        "execution_mode": "replay",
        "provenance": _provenance(),
        "received_at": BASE_TIME + timedelta(seconds=8),
    }
    return (
        _event(
            1,
            "campaign.created",
            "campaign",
            CAMPAIGN_ID,
            1,
            {
                "name": "replay test",
                "environment_id": "her",
                "adapter_version": "1",
                "data_origin": "synthetic",
                "execution_mode": "replay",
                "declaration": {"budget": {"hard_budget": "10", "budget_unit": "credit"}},
                "declaration_hash": "b" * 64,
                "state": "active",
            },
        ),
        _event(
            2,
            "work_item.queued",
            "work_item",
            WORK_ITEM_ID,
            1,
            {"candidate_id": "cand:one", "candidate": candidate, "state": "queued"},
        ),
        _event(
            3,
            "job.enqueued",
            "job",
            JOB_ID,
            1,
            {**job_payload, "state": "available", "lease_generation": 0},
        ),
        _event(
            4,
            "budget.reserved",
            "budget",
            RESERVATION_ID,
            1,
            {
                "entry_id": RESERVATION_ID,
                "work_item_id": WORK_ITEM_ID,
                "job_id": JOB_ID,
                "attempt_id": None,
                "lease_generation": 1,
                "reservation_entry_id": None,
                "kind": "reserved",
                "amount": "3",
                "unit": "credit",
                "reason": "reserved before execution",
                "recorded_at": BASE_TIME + timedelta(seconds=4),
            },
        ),
        _event(5, "job.leased", "job", JOB_ID, 2, {**job_payload, "state": "leased"}),
        _event(
            6,
            "attempt.started",
            "attempt",
            ATTEMPT_ID,
            1,
            {
                "work_item_id": WORK_ITEM_ID,
                "job_id": JOB_ID,
                "ordinal": 1,
                "state": "running",
                "started_at": BASE_TIME + timedelta(seconds=6),
                "created_at": BASE_TIME + timedelta(seconds=6),
            },
        ),
        _event(7, "job.started", "job", JOB_ID, 3, {**job_payload, "state": "running"}),
        _event(
            8,
            "observation.accepted",
            "attempt",
            ATTEMPT_ID,
            2,
            {**observation, "status": "accepted", "status_reason": None},
        ),
        _event(
            9,
            "attempt.completed",
            "attempt",
            ATTEMPT_ID,
            3,
            {
                "work_item_id": WORK_ITEM_ID,
                "job_id": JOB_ID,
                "ordinal": 1,
                "campaign_id": CAMPAIGN_ID,
                "state": "succeeded",
                "status": "succeeded",
                "observation_id": OBSERVATION_ID,
                "failure": None,
                "cost": {},
                "data_origin": "synthetic",
                "execution_mode": "replay",
                "provenance": _provenance(),
                "started_at": BASE_TIME + timedelta(seconds=6),
                "finished_at": BASE_TIME + timedelta(seconds=9),
            },
        ),
        _event(
            10,
            "budget.consumed",
            "budget",
            RESERVATION_ID,
            2,
            {
                "entry_id": SETTLEMENT_ID,
                "work_item_id": WORK_ITEM_ID,
                "job_id": JOB_ID,
                "attempt_id": ATTEMPT_ID,
                "lease_generation": 1,
                "reservation_entry_id": RESERVATION_ID,
                "kind": "consumed",
                "amount": "3",
                "unit": "credit",
                "reason": "durable outcome",
                "recorded_at": BASE_TIME + timedelta(seconds=10),
            },
        ),
        _event(
            11,
            "budget.adjusted",
            "budget",
            RESERVATION_ID,
            3,
            {
                "entry_id": ADJUSTMENT_ID,
                "work_item_id": WORK_ITEM_ID,
                "job_id": JOB_ID,
                "attempt_id": ATTEMPT_ID,
                "lease_generation": 1,
                "reservation_entry_id": RESERVATION_ID,
                "kind": "adjusted_down",
                "amount": "1",
                "unit": "credit",
                "reason": "late actual cost",
                "recorded_at": BASE_TIME + timedelta(seconds=11),
            },
        ),
        _event(12, "job.succeeded", "job", JOB_ID, 4, job_payload),
        _event(
            13,
            "work_item.accepted",
            "work_item",
            WORK_ITEM_ID,
            2,
            {"state": "accepted", "reason": None},
        ),
        _event(
            14,
            "campaign.completed",
            "campaign",
            CAMPAIGN_ID,
            2,
            {"state": "completed", "reason": None},
        ),
    )


def test_reconstructs_immutable_terminal_state_with_receipts_and_budget_adjustments() -> None:
    replay = reconstruct_campaign(_stream(), contract_version=2)

    assert replay.campaign.state == "completed"
    assert replay.work_items[0].state == "accepted"
    assert replay.jobs[0].state == "succeeded"
    assert replay.attempts[0].outcome is not None
    assert replay.attempts[0].outcome.status == "succeeded"
    assert replay.observations[0].status == "accepted"
    assert replay.budget.reserved == RESERVED_TOTAL
    assert replay.budget.consumed == CONSUMED_TOTAL
    assert replay.budget.adjusted_down == 1
    with pytest.raises(ValidationError):
        replay.campaign.state = "active"  # type: ignore[misc]


def test_a_semantic_gap_is_refused_even_when_envelopes_are_structurally_valid() -> None:
    stream = tuple(event for event in _stream() if event.event_type != "observation.accepted")

    with pytest.raises(SemanticIncompleteEventStreamError) as caught:
        reconstruct_campaign(stream, contract_version=2)

    assert caught.value.code == "semantic_incomplete_event_stream"
    assert OBSERVATION_ID in str(caught.value)


def test_contract_one_is_refused_for_full_phase7_reconstruction() -> None:
    with pytest.raises(SemanticIncompleteEventStreamError) as caught:
        reconstruct_campaign(_stream(), contract_version=1)

    assert "contract version 2" in str(caught.value)


def test_exported_stream_validation_reuses_registry_identity_and_order_rules() -> None:
    exported = [event.model_dump(mode="json") for event in _stream()]
    exported[1]["campaign_position"] = 3

    with pytest.raises(CampaignPositionGapError):
        validate_exported_stream(exported, campaign_id=CAMPAIGN_ID)


def _replace_event(
    stream: tuple[EventEnvelope, ...],
    position: int,
    *,
    event_type: str | None = None,
    payload: dict[str, object] | None = None,
) -> tuple[EventEnvelope, ...]:
    return tuple(
        event.model_copy(
            update={
                **({"event_type": event_type} if event_type is not None else {}),
                **({"payload": payload} if payload is not None else {}),
            }
        )
        if event.campaign_position == position
        else event
        for event in stream
    )


@pytest.mark.parametrize(
    ("_name", "invalid_stream"),
    [
        (
            "job starts without enqueue",
            lambda stream: _replace_event(
                stream,
                3,
                event_type="job.leased",
                payload={**stream[2].payload, "state": "leased"},
            ),
        ),
        (
            "job transition is illegal",
            lambda stream: _replace_event(
                stream,
                5,
                event_type="job.succeeded",
                payload={**stream[4].payload, "state": "succeeded"},
            ),
        ),
        (
            "attempt completion changes work item",
            lambda stream: _replace_event(
                stream,
                9,
                payload={**stream[8].payload, "work_item_id": uuid.uuid4()},
            ),
        ),
        (
            "attempt completion changes job",
            lambda stream: _replace_event(
                stream,
                9,
                payload={**stream[8].payload, "job_id": uuid.uuid4()},
            ),
        ),
        (
            "attempt completion changes ordinal",
            lambda stream: _replace_event(
                stream,
                9,
                payload={**stream[8].payload, "ordinal": 2},
            ),
        ),
        (
            "observation changes work item",
            lambda stream: _replace_event(
                stream,
                8,
                payload={**stream[7].payload, "work_item_id": uuid.uuid4()},
            ),
        ),
        (
            "succeeded outcome only has retained receipt",
            lambda stream: _replace_event(
                stream,
                8,
                event_type="observation.retained",
                payload={**stream[7].payload, "status": "received"},
            ),
        ),
        (
            "accepted work item has no succeeded outcome",
            lambda stream: _replace_event(
                _replace_event(
                    stream,
                    8,
                    event_type="observation.retained",
                    payload={**stream[7].payload, "status": "received"},
                ),
                9,
                payload={
                    **stream[8].payload,
                    "state": "duplicate_suppressed",
                    "status": "duplicate_suppressed",
                },
            ),
        ),
        (
            "completed campaign has nonterminal work item",
            lambda stream: tuple(
                event for event in stream if event.event_type != "work_item.accepted"
            ),
        ),
        (
            "reservation names unknown job",
            lambda stream: _replace_event(
                stream,
                4,
                payload={**stream[3].payload, "job_id": uuid.uuid4()},
            ),
        ),
        (
            "settlement changes lease generation",
            lambda stream: _replace_event(
                stream,
                10,
                payload={**stream[9].payload, "lease_generation": 2},
            ),
        ),
        (
            "settlement names unknown attempt",
            lambda stream: _replace_event(
                stream,
                10,
                payload={**stream[9].payload, "attempt_id": uuid.uuid4()},
            ),
        ),
        (
            "adjustment precedes settlement",
            lambda stream: tuple(
                event for event in stream if event.event_type != "budget.consumed"
            ),
        ),
    ],
)
def test_semantically_impossible_streams_fail_closed(
    _name: str,
    invalid_stream: Callable[[tuple[EventEnvelope, ...]], tuple[EventEnvelope, ...]],
) -> None:
    with pytest.raises(SemanticIncompleteEventStreamError):
        reconstruct_campaign(invalid_stream(_stream()), contract_version=2)

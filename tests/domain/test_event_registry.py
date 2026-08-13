from __future__ import annotations

import uuid

import pytest

from labbridge.domain import events

REGISTERED_EVENT_TYPES = {
    "campaign.created",
    "campaign.started",
    "campaign.paused",
    "campaign.resumed",
    "campaign.completed",
    "campaign.cancelled",
    "campaign.failed",
    "campaign.budget_exhausted",
    "work_item.queued",
    "work_item.accepted",
    "work_item.quarantined",
    "work_item.rejected",
    "work_item.cancelled",
    "job.enqueued",
    "job.leased",
    "job.started",
    "job.heartbeat",
    "job.lease_expired",
    "job.available",
    "job.retry_scheduled",
    "job.succeeded",
    "job.failed_terminal",
    "job.timed_out",
    "job.cancelled",
    "attempt.started",
    "attempt.completed",
    "observation.accepted",
    "observation.retained",
    "observation.invalidated",
    "observation.superseded",
    "budget.reserved",
    "budget.consumed",
    "budget.released",
    "budget.adjusted",
}


def _campaign_payload() -> dict[str, object]:
    return {
        "name": "typed stream",
        "environment_id": "her",
        "adapter_version": "1",
        "data_origin": "synthetic",
        "execution_mode": "replay",
        "declaration": {"candidates": []},
        "declaration_hash": "a" * 64,
        "state": "active",
    }


def _attempt_completed_payload() -> dict[str, object]:
    return {
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
        "started_at": "2026-08-01T00:00:00Z",
        "finished_at": "2026-08-01T00:00:01Z",
    }


def test_the_registry_contains_every_contract_event_at_version_one() -> None:
    registered = getattr(events, "registered_event_types", None)

    assert registered is not None
    assert registered() == {(event_type, 1) for event_type in REGISTERED_EVENT_TYPES}


def test_a_registered_payload_is_validated_and_serialised() -> None:
    validate = getattr(events, "validate_event_payload", None)

    assert validate is not None
    assert (
        validate(
            event_type="campaign.created",
            schema_version=1,
            aggregate_type="campaign",
            causation_id=None,
            payload=_campaign_payload(),
        )
        == _campaign_payload()
    )


def test_an_unknown_event_type_fails_explicitly() -> None:
    validate = getattr(events, "validate_event_payload", None)

    assert validate is not None
    with pytest.raises(ValueError) as caught:
        validate(
            event_type="campaign.unknown",
            schema_version=1,
            aggregate_type="campaign",
            causation_id=uuid.uuid4(),
            payload={},
        )

    assert type(caught.value).__name__ == "UnknownEventTypeError"
    assert caught.value.code == "unknown_event_type"


def test_an_unsupported_schema_version_fails_explicitly() -> None:
    validate = getattr(events, "validate_event_payload", None)
    unsupported_version = 2

    assert validate is not None
    with pytest.raises(events.UnsupportedEventVersionError) as caught:
        validate(
            event_type="campaign.created",
            schema_version=unsupported_version,
            aggregate_type="campaign",
            causation_id=None,
            payload=_campaign_payload(),
        )

    assert caught.value.schema_version == unsupported_version


def test_a_malformed_payload_fails_explicitly() -> None:
    validate = getattr(events, "validate_event_payload", None)
    payload = _campaign_payload()
    payload.pop("declaration_hash")

    assert validate is not None
    with pytest.raises(ValueError) as caught:
        validate(
            event_type="campaign.created",
            schema_version=1,
            aggregate_type="campaign",
            causation_id=None,
            payload=payload,
        )

    assert type(caught.value).__name__ == "InvalidEventPayloadError"
    assert caught.value.code == "invalid_event_payload"


def test_an_event_cannot_be_written_under_the_wrong_aggregate_type() -> None:
    validate = getattr(events, "validate_event_payload", None)

    assert validate is not None
    with pytest.raises(ValueError) as caught:
        validate(
            event_type="campaign.created",
            schema_version=1,
            aggregate_type="work_item",
            causation_id=None,
            payload=_campaign_payload(),
        )

    assert type(caught.value).__name__ == "EventAggregateTypeError"
    assert caught.value.code == "event_aggregate_type_mismatch"


def test_only_campaign_created_may_omit_causation() -> None:
    validate = getattr(events, "validate_event_payload", None)

    assert validate is not None
    with pytest.raises(ValueError) as caught:
        validate(
            event_type="campaign.started",
            schema_version=1,
            aggregate_type="campaign",
            causation_id=None,
            payload={"state": "active", "reason": None},
        )

    assert type(caught.value).__name__ == "MissingEventCausationError"
    assert caught.value.code == "event_causation_required"


def test_an_accepted_observation_event_requires_a_matching_provenance_root() -> None:
    validate = getattr(events, "validate_event_payload", None)

    assert validate is not None
    with pytest.raises(ValueError) as caught:
        validate(
            event_type="observation.accepted",
            schema_version=1,
            aggregate_type="attempt",
            causation_id=uuid.uuid4(),
            payload={
                "observation_id": "obs:test",
                "work_item_id": uuid.uuid4(),
                "attempt_id": uuid.uuid4(),
                "media_type": "text/csv",
                "object_uri": "s3://labbridge/test",
                "byte_size": 1,
                "sha256": "a" * 64,
                "schema_version": "1",
                "signal_kind": "lsv",
                "quantities": [],
                "status": "accepted",
                "status_reason": None,
                "data_origin": "observed",
                "execution_mode": "replay",
                "provenance": {
                    "environment": {
                        "environment_id": "her",
                        "adapter_version": "1",
                        "data_origin": "synthetic",
                        "execution_mode": "replay",
                    },
                    "synthetic_root": {
                        "generator": "fixture",
                        "generator_version": "1",
                        "seed": 0,
                        "config_hash": "config",
                    },
                    "code_version": "1",
                    "config_hash": "config",
                },
                "received_at": "2026-08-01T00:00:00Z",
            },
        )

    assert type(caught.value).__name__ == "InvalidEventPayloadError"


def test_retained_observation_and_budget_adjustment_are_registered_facts() -> None:
    causation_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    work_item_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    retained = {
        "observation_id": "obs:test",
        "work_item_id": work_item_id,
        "attempt_id": attempt_id,
        "media_type": "text/csv",
        "object_uri": "s3://labbridge/test",
        "byte_size": 1,
        "sha256": "a" * 64,
        "schema_version": "1",
        "signal_kind": "lsv",
        "quantities": [],
        "status": "received",
        "status_reason": "refused after receipt",
        "data_origin": "synthetic",
        "execution_mode": "replay",
        "provenance": {
            "environment": {
                "environment_id": "her",
                "adapter_version": "1",
                "data_origin": "synthetic",
                "execution_mode": "replay",
            },
            "synthetic_root": {
                "generator": "fixture",
                "generator_version": "1",
                "seed": 0,
                "config_hash": "config",
            },
            "code_version": "1",
            "config_hash": "config",
        },
        "received_at": "2026-08-01T00:00:00Z",
    }

    assert (
        events.validate_event_payload(
            event_type="observation.retained",
            schema_version=1,
            aggregate_type="attempt",
            causation_id=causation_id,
            payload=retained,
        )["status"]
        == "received"
    )
    assert (
        events.validate_event_payload(
            event_type="budget.adjusted",
            schema_version=1,
            aggregate_type="budget",
            causation_id=causation_id,
            payload={
                "entry_id": uuid.uuid4(),
                "work_item_id": work_item_id,
                "job_id": uuid.uuid4(),
                "attempt_id": attempt_id,
                "lease_generation": 1,
                "reservation_entry_id": reservation_id,
                "kind": "adjusted_down",
                "amount": "1",
                "unit": "credit",
                "reason": "late actual cost",
                "recorded_at": "2026-08-01T00:00:00Z",
            },
        )["kind"]
        == "adjusted_down"
    )


@pytest.mark.parametrize(
    ("event_type", "aggregate_type", "payload"),
    [
        ("campaign.started", "campaign", {"state": "failed", "reason": "wrong fact"}),
        (
            "work_item.queued",
            "work_item",
            {
                "candidate_id": "cand:test",
                "candidate": {
                    "kind": "her_location",
                    "library_id": "library",
                    "measurement_area_id": "area",
                    "grid_x": {"value": "1", "unit": "mm"},
                    "grid_y": {"value": "2", "unit": "mm"},
                },
                "state": "rejected",
            },
        ),
    ],
)
def test_an_event_name_cannot_describe_a_different_projection_state(
    event_type: str, aggregate_type: str, payload: dict[str, object]
) -> None:
    with pytest.raises(events.InvalidEventPayloadError):
        events.validate_event_payload(
            event_type=event_type,
            schema_version=1,
            aggregate_type=aggregate_type,
            causation_id=uuid.uuid4(),
            payload=payload,
        )


def test_a_terminal_attempt_event_reuses_outcome_invariants() -> None:
    payload = _attempt_completed_payload()
    payload["failure"] = None

    with pytest.raises(events.InvalidEventPayloadError):
        events.validate_event_payload(
            event_type="attempt.completed",
            schema_version=1,
            aggregate_type="attempt",
            causation_id=uuid.uuid4(),
            payload=payload,
        )


def test_a_failed_campaign_event_requires_a_reason() -> None:
    with pytest.raises(events.InvalidEventPayloadError):
        events.validate_event_payload(
            event_type="campaign.failed",
            schema_version=1,
            aggregate_type="campaign",
            causation_id=uuid.uuid4(),
            payload={"state": "failed", "reason": None},
        )


def test_an_attempt_outcome_always_matches_its_provenance_environment() -> None:
    payload = _attempt_completed_payload()
    payload["data_origin"] = "observed"

    with pytest.raises(events.InvalidEventPayloadError):
        events.validate_event_payload(
            event_type="attempt.completed",
            schema_version=1,
            aggregate_type="attempt",
            causation_id=uuid.uuid4(),
            payload=payload,
        )


def test_an_accepted_event_cannot_carry_an_invalidated_observation() -> None:
    payload = {
        "observation_id": "obs:test",
        "work_item_id": uuid.uuid4(),
        "attempt_id": uuid.uuid4(),
        "media_type": "text/csv",
        "object_uri": "s3://labbridge/test",
        "byte_size": 1,
        "sha256": "a" * 64,
        "schema_version": "1",
        "signal_kind": "lsv",
        "quantities": [],
        "status": "invalidated",
        "status_reason": "superseded",
        "data_origin": "synthetic",
        "execution_mode": "replay",
        "provenance": {
            "environment": {
                "environment_id": "her",
                "adapter_version": "1",
                "data_origin": "synthetic",
                "execution_mode": "replay",
            },
            "synthetic_root": {
                "generator": "fixture",
                "generator_version": "1",
                "seed": 0,
                "config_hash": "config",
            },
            "code_version": "1",
            "config_hash": "config",
        },
        "received_at": "2026-08-01T00:00:00Z",
    }

    with pytest.raises(events.InvalidEventPayloadError):
        events.validate_event_payload(
            event_type="observation.accepted",
            schema_version=1,
            aggregate_type="attempt",
            causation_id=uuid.uuid4(),
            payload=payload,
        )


def test_an_observation_relation_is_typed_and_non_reflexive() -> None:
    observation_id = "obs:test"

    with pytest.raises(events.InvalidEventPayloadError):
        events.validate_event_payload(
            event_type="observation.invalidated",
            schema_version=1,
            aggregate_type="campaign",
            causation_id=uuid.uuid4(),
            payload={
                "relation_id": uuid.uuid4(),
                "subject_id": observation_id,
                "predicate": "invalidates",
                "object_id": observation_id,
                "reason": "bad calibration",
                "recorded_at": "2026-08-01T00:00:00Z",
            },
        )

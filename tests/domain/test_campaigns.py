from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from labbridge.domain.campaigns import (
    CampaignDeclaration,
    RetryPolicy,
    retryable_failure,
)


def _declaration(**overrides: object) -> CampaignDeclaration:
    values: dict[str, object] = {
        "hard_budget": Decimal("10"),
        "per_attempt_estimate": Decimal("2.5"),
        "budget_unit": "attempt-credit",
        "max_attempts": 3,
        "stopping_rule": "hard_budget_exhausted",
    }
    values.update(overrides)
    return CampaignDeclaration.model_validate(values)


def test_campaign_declaration_is_frozen_and_keeps_exact_decimal_costs() -> None:
    declaration = _declaration()

    assert declaration.hard_budget == Decimal("10")
    assert declaration.per_attempt_estimate == Decimal("2.5")
    with pytest.raises(ValidationError):
        declaration.max_attempts = 4  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hard_budget", Decimal("0")),
        ("hard_budget", Decimal("-1")),
        ("hard_budget", Decimal("NaN")),
        ("per_attempt_estimate", Decimal("0")),
        ("per_attempt_estimate", Decimal("Infinity")),
        ("budget_unit", "   "),
        ("max_attempts", 0),
        ("stopping_rule", "no_improvement"),
        ("stopping_rule", "maximum_accepted_observations"),
        ("stopping_rule", "target_metric_reached"),
    ],
)
def test_campaign_declaration_rejects_invalid_budget_policy(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _declaration(**{field: value})


def test_one_attempt_must_fit_inside_the_hard_budget() -> None:
    with pytest.raises(ValidationError, match="per_attempt_estimate"):
        _declaration(hard_budget=Decimal("1"), per_attempt_estimate=Decimal("2"))


@pytest.mark.parametrize(
    ("failure_code", "expected"),
    [
        ("timeout", True),
        ("object_store_unavailable", True),
        ("database_conflict", True),
        ("lease_lost", True),
        ("adapter_transient", True),
        ("unsupported_schema", False),
        ("checksum_mismatch", False),
        ("invalid_unit", False),
        ("source_unavailable", False),
        ("unknown_failure_code", False),
    ],
)
def test_retryability_is_decided_by_stable_failure_code(failure_code: str, expected: bool) -> None:
    assert retryable_failure(failure_code) is expected


def test_untyped_adapter_error_is_not_retryable() -> None:
    assert retryable_failure("adapter_error") is False


def test_retry_backoff_is_bounded_and_does_not_sleep() -> None:
    policy = RetryPolicy(base_seconds=2, maximum_seconds=10)

    assert [policy.backoff_seconds(attempt) for attempt in (1, 2, 3, 4, 20)] == [2, 4, 8, 10, 10]

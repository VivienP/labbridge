"""State machines and typed quantities.

The transition tests are exhaustive rather than illustrative: every state pair in each machine is
checked against its table, so a state added without transitions, or a transition quietly widened,
fails here.
"""

from __future__ import annotations

import itertools
from decimal import Decimal

import pytest
from pydantic import ValidationError

from labbridge.domain.lifecycle import (
    ATTEMPT_TRANSITIONS,
    CAMPAIGN_TRANSITIONS,
    JOB_TRANSITIONS,
    WORK_ITEM_TRANSITIONS,
    IllegalTransitionError,
    check_attempt_transition,
    check_campaign_transition,
    check_job_transition,
    check_work_item_transition,
    terminal_states,
)
from labbridge.domain.quantities import (
    DIMENSIONLESS,
    UNKNOWN_UNIT,
    CostRecord,
    Money,
    Quantity,
    Uncertainty,
)

MACHINES = [
    ("campaign", CAMPAIGN_TRANSITIONS, check_campaign_transition),
    ("work_item", WORK_ITEM_TRANSITIONS, check_work_item_transition),
    ("attempt", ATTEMPT_TRANSITIONS, check_attempt_transition),
    ("job", JOB_TRANSITIONS, check_job_transition),
]


@pytest.mark.parametrize(("name", "table", "check"), MACHINES)
def test_every_state_pair_agrees_with_its_table(name: str, table: dict, check) -> None:  # type: ignore[type-arg,no-untyped-def]
    for source, target in itertools.product(table, table):
        if target in table[source]:
            check(source, target)
            continue
        with pytest.raises(IllegalTransitionError):
            check(source, target)


@pytest.mark.parametrize(("name", "table", "check"), MACHINES)
def test_terminal_states_have_no_way_out(name: str, table: dict, check) -> None:  # type: ignore[type-arg,no-untyped-def]
    terminals = terminal_states(table)
    assert terminals, f"{name} has no terminal state"
    for terminal, target in itertools.product(terminals, table):
        with pytest.raises(IllegalTransitionError):
            check(terminal, target)


def test_budget_exhausted_is_terminal_not_an_error_path() -> None:
    """docs/SPEC.md section 7.1 calls it a valid terminal state, not an exception."""
    assert "budget_exhausted" in terminal_states(CAMPAIGN_TRANSITIONS)
    check_campaign_transition("active", "budget_exhausted")


def test_a_quarantined_work_item_can_only_return_through_the_queue() -> None:
    """Release is a deliberate act; a quarantined item must not jump straight to accepted."""
    check_work_item_transition("quarantined", "queued")
    with pytest.raises(IllegalTransitionError):
        check_work_item_transition("quarantined", "accepted")


def test_a_retry_is_a_new_attempt_not_a_reopened_one() -> None:
    """Section 7.3: a retry creates a new attempt; the failed one stays failed."""
    for terminal in ("failed_retryable", "succeeded", "corrupted"):
        with pytest.raises(IllegalTransitionError):
            check_attempt_transition(terminal, "running")  # type: ignore[arg-type]


def test_an_expired_lease_returns_a_job_to_available() -> None:
    check_job_transition("leased", "available")
    check_job_transition("running", "available")


def test_the_illegal_transition_error_names_both_states() -> None:
    with pytest.raises(IllegalTransitionError) as caught:
        check_campaign_transition("completed", "active")

    assert caught.value.source == "completed"
    assert caught.value.target == "active"


def test_a_quantity_requires_a_unit() -> None:
    with pytest.raises(ValidationError):
        Quantity(value=Decimal("1"), unit="")


def test_an_unknown_unit_is_recordable_and_visible() -> None:
    """F-015: an unknown unit stays explicitly unknown rather than being guessed."""
    quantity = Quantity(value=Decimal("1"), unit=UNKNOWN_UNIT)

    assert quantity.is_unknown_unit
    assert not Quantity(value=Decimal("1"), unit=DIMENSIONLESS).is_unknown_unit


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_quantity_is_rejected(literal: str) -> None:
    """Enforced by Pydantic's Decimal handling. Asserted here so a config change cannot silently
    start admitting a value that has no canonical form and therefore no identity."""
    with pytest.raises(ValidationError, match="finite"):
        Quantity(value=Decimal(literal), unit="V")


def test_a_non_finite_value_inside_an_array_is_rejected() -> None:
    with pytest.raises(ValidationError, match="finite"):
        Quantity(value=(Decimal("1"), Decimal("NaN")), unit="V")


def test_a_confidence_interval_requires_its_level() -> None:
    with pytest.raises(ValidationError, match="requires an explicit confidence_level"):
        Uncertainty(kind="confidence_interval", value=Quantity(value=Decimal("1"), unit="V"))


def test_an_unknown_uncertainty_carries_no_value() -> None:
    with pytest.raises(ValidationError, match="carries no value"):
        Uncertainty(kind="unknown", value=Quantity(value=Decimal("1"), unit="V"))

    assert Uncertainty(kind="unknown").value is None


def test_money_requires_a_currency_code() -> None:
    with pytest.raises(ValidationError):
        Money(amount=Decimal("1"), currency="")

    assert Money(amount=Decimal("1.00"), currency="EUR").currency == "EUR"


def test_an_empty_cost_record_is_valid_because_a_replay_costs_no_consumables() -> None:
    assert CostRecord().consumable_cost is None

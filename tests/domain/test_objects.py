"""The object classification rule, enumerated offline.

Reconciliation runs when something has already gone wrong, which is the worst moment to discover the
rule is ambiguous. Every branch is reachable from a fact combination here, and every verdict states
which fact produced it — so a reviewer can check the reasoning without a database, a store, or a
crash.

The one property that matters more than any individual verdict: **nothing here deletes anything.**
That is asserted directly, because it is the rule most likely to be quietly relaxed by someone
tidying up orphans.
"""

from __future__ import annotations

import pytest

from labbridge.domain.objects import (
    IMMUTABLE_CLASSIFICATIONS,
    OBJECT_CLASSIFICATIONS,
    ObjectFacts,
    classify_object,
)

ACCEPTED_DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


def _facts(**overrides: object) -> ObjectFacts:
    defaults: dict[str, object] = {
        "state": "committed",
        "exists": True,
        "recorded_sha256": ACCEPTED_DIGEST,
        "actual_sha256": ACCEPTED_DIGEST,
        "referenced_by_accepted": True,
        "outcome_status": "succeeded",
    }
    defaults.update(overrides)
    return ObjectFacts(**defaults)  # type: ignore[arg-type]


def test_an_object_backing_an_accepted_observation_is_accepted_evidence() -> None:
    verdict = classify_object(_facts())

    assert verdict.classification == "accepted_evidence"
    assert "accepted observation" in verdict.reason


def test_a_committed_row_whose_object_vanished_is_missing() -> None:
    """The strongest signal there is that something was lost, so it must not be filed as an orphan
    alongside bytes that are merely unreferenced."""
    verdict = classify_object(_facts(exists=False, actual_sha256=None))

    assert verdict.classification == "missing"
    assert "committed evidence" in verdict.reason


def test_a_staged_object_that_never_landed_is_missing_and_says_so_differently() -> None:
    verdict = classify_object(
        _facts(state="pending", exists=False, actual_sha256=None, referenced_by_accepted=False)
    )

    assert verdict.classification == "missing"
    assert "never landed" in verdict.reason


def test_a_checksum_disagreement_is_quarantined_and_neither_side_is_trusted() -> None:
    """Refreshing the recorded digest would launder corruption into evidence; deleting the object
    would destroy the proof it happened. Both are refused (F-028)."""
    verdict = classify_object(_facts(actual_sha256=OTHER_DIGEST))

    assert verdict.classification == "quarantined"
    assert ACCEPTED_DIGEST in verdict.reason
    assert OTHER_DIGEST in verdict.reason


def test_a_checksum_disagreement_outranks_being_referenced() -> None:
    """Otherwise an accepted reference would launder a mismatch into `accepted_evidence`, which is
    the exact direction the error must never resolve."""
    verdict = classify_object(_facts(actual_sha256=OTHER_DIGEST, referenced_by_accepted=True))

    assert verdict.classification == "quarantined"


def test_bytes_from_a_suppressed_duplicate_are_kept_as_diagnostic_duplicate() -> None:
    """Reached only when the bytes differ from the accepted object: identical bytes share the
    content-addressed key and are already the accepted row."""
    verdict = classify_object(
        _facts(
            state="pending",
            recorded_sha256=OTHER_DIGEST,
            actual_sha256=OTHER_DIGEST,
            referenced_by_accepted=False,
            outcome_status="duplicate_suppressed",
        )
    )

    assert verdict.classification == "diagnostic_duplicate"
    assert "differ" in verdict.reason
    assert "not promoted" in verdict.reason


def test_bytes_whose_transaction_never_committed_are_a_diagnostic_orphan() -> None:
    verdict = classify_object(
        _facts(state="pending", referenced_by_accepted=False, outcome_status=None)
    )

    assert verdict.classification == "diagnostic_orphan"
    assert "never committed" in verdict.reason


def test_bytes_from_a_lease_lost_execution_are_a_diagnostic_orphan_naming_the_outcome() -> None:
    verdict = classify_object(
        _facts(state="pending", referenced_by_accepted=False, outcome_status="lease_lost")
    )

    assert verdict.classification == "diagnostic_orphan"
    assert "lease_lost" in verdict.reason


def test_every_verdict_states_a_reason() -> None:
    """A classification without evidence is not inspectable, which is the whole requirement."""
    combinations = [
        _facts(),
        _facts(exists=False, actual_sha256=None),
        _facts(actual_sha256=OTHER_DIGEST),
        _facts(state="pending", referenced_by_accepted=False, outcome_status=None),
        _facts(
            state="pending",
            recorded_sha256=OTHER_DIGEST,
            actual_sha256=OTHER_DIGEST,
            referenced_by_accepted=False,
            outcome_status="duplicate_suppressed",
        ),
    ]

    for facts in combinations:
        verdict = classify_object(facts)
        assert verdict.reason.strip()
        assert verdict.classification in OBJECT_CLASSIFICATIONS


def test_the_rule_is_deterministic_for_the_same_facts() -> None:
    """Reproducibility is a stated requirement of classification, not an implementation detail."""
    facts = _facts(state="pending", referenced_by_accepted=False, outcome_status=None)

    assert classify_object(facts) == classify_object(facts)


@pytest.mark.parametrize("classification", OBJECT_CLASSIFICATIONS)
def test_no_verdict_asks_for_deletion(classification: str) -> None:
    """Deletion is never a recovery action. Asserted over the vocabulary rather than the prose, so
    adding a `deleted` verdict fails here rather than in review."""
    assert "delet" not in classification
    assert "purge" not in classification
    assert "remove" not in classification


def test_accepted_and_quarantined_bytes_are_declared_immutable() -> None:
    """One backs a released claim, the other is the record of something unexplained. Losing either
    to a tidy-up is the failure this set exists to prevent."""
    assert {"accepted_evidence", "quarantined"} == IMMUTABLE_CLASSIFICATIONS
    assert set(OBJECT_CLASSIFICATIONS) >= IMMUTABLE_CLASSIFICATIONS

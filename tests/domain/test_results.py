"""Observations, outcomes, and metrics: the shapes that must be unconstructable.

ADR-005 keeps received bytes. Invariant 3 forbids an unversioned derived value. §3.5 allows an
observation only on an outcome that actually received bytes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from labbridge.domain.identity import EnvironmentRef
from labbridge.domain.provenance import Provenance, SyntheticRoot
from labbridge.domain.quantities import Quantity
from labbridge.domain.results import (
    AttemptOutcome,
    DerivedMetric,
    FailureRecord,
    Observation,
    QuantityDescriptor,
    metric_id,
    observation_id,
)

NOW = datetime(2026, 7, 30, tzinfo=UTC)
SHA = "b" * 64
ENVIRONMENT = EnvironmentRef(
    environment_id="her", adapter_version="1", data_origin="synthetic", execution_mode="replay"
)
PROVENANCE = Provenance(
    environment=ENVIRONMENT,
    synthetic_root=SyntheticRoot(
        generator="fixture", generator_version="0.1.0", seed=7, config_hash="x"
    ),
    code_version="1",
    config_hash="x",
)
DESCRIPTORS = (
    QuantityDescriptor(name="potential", unit="V", axis=0, length=208),
    QuantityDescriptor(name="current_density", unit="A/cm^2", axis=1, length=208),
)


def _observation(status: str = "received", reason: str | None = None) -> Observation:
    return Observation(
        observation_id="obs:1",
        campaign_id="camp:1",
        work_item_id="wi:1",
        attempt_id="att:1",
        media_type="text/csv",
        object_uri="s3://labbridge/obs/1",
        byte_size=10,
        sha256=SHA,
        schema_version="1",
        signal_kind="lsv",
        quantities=DESCRIPTORS,
        status=status,  # type: ignore[arg-type]
        received_at=NOW,
        provenance=PROVENANCE,
        status_reason=reason,
    )


def _outcome(status: str, **overrides: object) -> AttemptOutcome:
    payload: dict[str, object] = {
        "attempt_id": "att:1",
        "work_item_id": "wi:1",
        "status": status,
        "finished_at": NOW,
        "provenance": PROVENANCE,
    }
    payload.update(overrides)
    return AttemptOutcome(**payload)  # type: ignore[arg-type]


def test_a_corrupted_observation_is_constructible_and_keeps_its_bytes() -> None:
    """ADR-005: if bytes arrived, an observation exists. Persist first, classify second."""
    observation = _observation("corrupted", reason="array length mismatch")

    assert observation.status == "corrupted"
    assert observation.sha256 == SHA


@pytest.mark.parametrize("status", ["corrupted", "invalidated"])
def test_a_rejected_observation_without_a_reason_is_rejected(status: str) -> None:
    with pytest.raises(ValidationError, match="requires a status_reason"):
        _observation(status)


def test_the_observation_identity_covers_units() -> None:
    """Two observations differing only in a unit are different observations."""
    base = observation_id(
        sha256=SHA,
        schema_version="1",
        signal_kind="lsv",
        quantities=DESCRIPTORS,
        provenance=PROVENANCE,
    )
    other = observation_id(
        sha256=SHA,
        schema_version="1",
        signal_kind="lsv",
        quantities=(
            DESCRIPTORS[0],
            QuantityDescriptor(name="current_density", unit="mA/cm^2", axis=1, length=208),
        ),
        provenance=PROVENANCE,
    )

    assert base != other


def test_the_observation_identity_covers_the_schema_version() -> None:
    base = observation_id(
        sha256=SHA,
        schema_version="1",
        signal_kind="lsv",
        quantities=DESCRIPTORS,
        provenance=PROVENANCE,
    )
    bumped = observation_id(
        sha256=SHA,
        schema_version="2",
        signal_kind="lsv",
        quantities=DESCRIPTORS,
        provenance=PROVENANCE,
    )

    assert base != bumped


def test_a_timed_out_outcome_cannot_carry_an_observation() -> None:
    """No bytes were received, so an observation on it would be a fabrication."""
    with pytest.raises(ValidationError, match="cannot carry an observation"):
        _outcome("timed_out", observation_id="obs:1")


def test_a_corrupted_outcome_may_carry_an_observation() -> None:
    assert _outcome("corrupted", observation_id="obs:1").observation_id == "obs:1"


def test_a_succeeded_outcome_carries_no_failure() -> None:
    failure = FailureRecord(failure_code="x", category="transport", retryable=True, summary="s")
    with pytest.raises(ValidationError, match="carries no failure"):
        _outcome("succeeded", failure=failure)


def test_a_failed_outcome_requires_a_failure_record() -> None:
    with pytest.raises(ValidationError, match="requires a failure record"):
        _outcome("failed_terminal")


def test_a_terminal_failure_may_not_be_marked_retryable() -> None:
    """The status and the record must agree, or a retry loop and an operator disagree."""
    failure = FailureRecord(
        failure_code="x", category="schema", retryable=True, summary="unsupported schema"
    )
    with pytest.raises(ValidationError, match="requires failure"):
        _outcome("failed_terminal", failure=failure)


def _metric(status: str = "accepted", reason: str | None = None) -> DerivedMetric:
    return DerivedMetric(
        metric_id="metric:1",
        observation_id="obs:1",
        name="tafel_slope",
        value=Quantity(value=Decimal("118"), unit="mV"),
        analysis_name="labbridge_tafel",
        analysis_version="1",
        parameter_hash="abc",
        quality_status=status,  # type: ignore[arg-type]
        provenance=PROVENANCE,
        quality_reason=reason,
    )


def test_a_rejected_metric_must_state_why() -> None:
    with pytest.raises(ValidationError, match="requires a quality_reason"):
        _metric("rejected")


def test_a_metric_cannot_be_built_without_an_analysis_version() -> None:
    """Invariant 3: an unversioned derived value cannot be reinterpreted later."""
    with pytest.raises(ValidationError):
        DerivedMetric(
            metric_id="metric:1",
            observation_id="obs:1",
            name="tafel_slope",
            value=Quantity(value=Decimal("118"), unit="mV"),
            analysis_name="labbridge_tafel",
            analysis_version="",
            parameter_hash="abc",
            quality_status="accepted",
            provenance=PROVENANCE,
        )


def test_a_source_fit_and_a_labbridge_recomputation_get_different_identities() -> None:
    """docs/SPEC.md section 3.6: they must never merge into one metric."""
    source = metric_id(
        observation_id="obs:1",
        attempt_id="att:1",
        name="i_lim",
        analysis_name="source_provided_fit",
        analysis_version="1",
        parameter_hash="abc",
    )
    recomputed = metric_id(
        observation_id="obs:1",
        attempt_id="att:1",
        name="i_lim",
        analysis_name="labbridge_fit",
        analysis_version="1",
        parameter_hash="abc",
    )

    assert source != recomputed


def test_recomputing_the_same_analysis_is_idempotent_by_identity() -> None:
    args = {
        "observation_id": "obs:1",
        "attempt_id": "att:1",
        "name": "i_lim",
        "analysis_name": "labbridge_fit",
        "analysis_version": "1",
        "parameter_hash": "abc",
    }

    assert metric_id(**args) == metric_id(**args)  # type: ignore[arg-type]


def test_two_receipts_of_identical_content_get_different_metric_identities() -> None:
    """`observation_id` is content-derived, so two campaigns replaying one location share it. A
    metric identity without the attempt would collide across campaigns."""
    shared = {
        "observation_id": "obs:1",
        "name": "i_lim",
        "analysis_name": "labbridge_fit",
        "analysis_version": "1",
        "parameter_hash": "abc",
    }

    first = metric_id(attempt_id="att:1", **shared)  # type: ignore[arg-type]
    second = metric_id(attempt_id="att:2", **shared)  # type: ignore[arg-type]

    assert first != second

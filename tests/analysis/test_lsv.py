"""The LSV metric: what it computes, and what it refuses to compute.

Every payload here is built inline rather than taken from the fixture, so each test states the exact
shape it is about. The fixture's job is to prove the *adapter* meets real structure; this file's job
is to pin the analysis's operational definition.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from labbridge.analysis.lsv import (
    ANALYSIS_NAME,
    ANALYSIS_VERSION,
    MIN_SAMPLES,
    SOURCE_FIT_ANALYSIS_NAME,
    LsvAnalysis,
    analyse,
    parameter_hash,
)
from labbridge.domain.quantities import UNKNOWN_UNIT

HEADER = b"Potential vs. RHE [V],Current density [A/cm^2],Standard deviation [A/cm^2]\r\n"
SAMPLE_COUNT = 200


def _sweep(*, floor: Decimal = Decimal("-5"), count: int = SAMPLE_COUNT) -> bytes:
    rows = []
    for index in range(count):
        fraction = Decimal(index) / Decimal(count - 1)
        potential = Decimal("0.01") - fraction * Decimal("0.91")
        current = floor * fraction * fraction
        rows.append(f"{potential},{current},0.01".encode())
    return HEADER + b"\r\n".join(rows) + b"\r\n"


def _analyse(payload: bytes) -> LsvAnalysis:
    return analyse(payload, potential_unit="V", current_unit="A/cm^2")


def test_the_extremum_is_the_most_cathodic_recorded_point() -> None:
    result = _analyse(_sweep(floor=Decimal("-7.5")))

    assert result.is_accepted
    assert result.current_extremum is not None
    assert result.current_extremum.value == Decimal("-7.5")
    assert result.current_extremum.unit == "A/cm^2"


def test_the_potential_reported_is_the_one_at_that_point() -> None:
    result = _analyse(_sweep())

    assert result.potential_at_extremum is not None
    assert result.potential_at_extremum.value < 0
    assert result.potential_at_extremum.unit == "V"


def test_the_area_basis_is_recorded_as_unknown_rather_than_assumed() -> None:
    """The source states no area basis. Guessing one would rescale every value silently and make
    two locations look comparable when they may not be."""
    assert _analyse(_sweep()).area_basis == UNKNOWN_UNIT


def test_a_truncated_file_is_rejected_with_its_reason() -> None:
    result = _analyse(_sweep(count=10))

    assert result.quality_status == "rejected"
    assert result.quality_reason is not None
    assert str(MIN_SAMPLES) in result.quality_reason
    assert result.current_extremum is None


def test_a_file_with_no_data_rows_is_rejected_not_crashed() -> None:
    result = _analyse(HEADER)

    assert result.quality_status == "rejected"
    assert result.sample_count == 0


def test_a_non_cathodic_sweep_warns_rather_than_being_discarded() -> None:
    """F-023: a poor but valid signal is still a successful observation. Discarding it would lose
    real data — 46 of the archive's 966 LSV files reach positive current near onset."""
    result = _analyse(_sweep(floor=Decimal("0.5")))

    assert result.quality_status == "warning"
    assert result.current_extremum is not None
    assert result.quality_reason is not None


def test_malformed_rows_are_skipped_and_the_rest_still_analysed() -> None:
    payload = _sweep().replace(b"0.01\r\n", b"0.01\r\nnot,a,number\r\n", 1)

    result = _analyse(payload)

    assert result.is_accepted
    assert result.sample_count == SAMPLE_COUNT


def test_a_rejected_file_reports_how_many_rows_were_malformed() -> None:
    payload = HEADER + b"\r\n".join([b"bad,row,here"] * 10) + b"\r\n"

    result = _analyse(payload)

    assert result.quality_reason is not None
    assert "malformed" in result.quality_reason


def test_the_parameter_hash_is_stable_across_calls() -> None:
    assert parameter_hash() == parameter_hash()


def test_the_labbridge_analysis_is_named_distinctly_from_the_source_fit() -> None:
    """docs/SPEC.md §3.6: a source-provided fit and a recomputation never merge into one metric."""
    assert ANALYSIS_NAME != SOURCE_FIT_ANALYSIS_NAME
    assert ANALYSIS_VERSION


@pytest.mark.parametrize("terminator", [b"\r\n", b"\n", b"\r"])
def test_every_line_ending_in_the_archive_parses(terminator: bytes) -> None:
    """CR, CRLF and LF all occur in the source. A CR-only file must not collapse to one row."""
    payload = _sweep().replace(b"\r\n", terminator)

    assert _analyse(payload).is_accepted

from __future__ import annotations

from decimal import Decimal

import pytest

from labbridge.domain.cv import ColumnMapping, CVImportProfile, CVMetadata
from labbridge.infrastructure.cv_csv import CsvParseError, parse_cv_csv


def _profile(
    *,
    delimiter: str = ",",
    decimal_convention: str = "point",
    missing: tuple[str, ...] = ("", "NA"),
) -> CVImportProfile:
    return CVImportProfile(
        schema_version="1",
        technique="cyclic_voltammetry",
        environment_id="synthetic_cv_fixture",
        encoding="utf-8",
        delimiter=delimiter,
        decimal_convention=decimal_convention,
        header_row=1,
        missing_value_tokens=missing,
        columns=(
            ColumnMapping(source_column="E", role="potential", source_unit="mV", target_unit="V"),
            ColumnMapping(source_column="I", role="current", source_unit="mA", target_unit="A"),
            ColumnMapping(source_column="note", role="ignored"),
        ),
        metadata=CVMetadata.unknown(),
    )


def test_parser_uses_the_declared_delimiter_decimal_and_units() -> None:
    parsed = parse_cv_csv(
        b"E;I;note\r\n-100,5;-2,5;synthetic\r\n",
        _profile(delimiter=";", decimal_convention="comma"),
    )

    assert parsed.row_count == 1
    assert parsed.series[0].source_values == (Decimal("-100.5"),)
    assert parsed.series[0].values == (Decimal("-0.1005"),)
    assert parsed.series[1].values == (Decimal("-0.0025"),)


@pytest.mark.parametrize(
    "payload,profile,error_code",
    [
        (b"E,I,note,extra\n0,1,x,2\n", _profile(), "unexpected_column"),
        (b"E,E,note\n0,1,x\n", _profile(), "duplicate_header"),
        (b"E,I,note\n0,nope,x\n", _profile(), "non_numeric_cell"),
        (b"E,I,note\n0,NaN,x\n", _profile(), "non_finite_value"),
        (b"E,I,note\n0,1\n", _profile(), "row_length_mismatch"),
        (b"E,I,note\n0,1,x\n\n1,2,y\n", _profile(), "blank_row"),
        (b"E,I,note\n0,NA,x\n", _profile(), "missing_scientific_value"),
        (b"E,I\n0,1\n", _profile(), "missing_column"),
        (b'E,I,note\n0,"unterminated,x\n', _profile(), "malformed_csv"),
    ],
)
def test_parser_fails_closed_for_invalid_tables(
    payload: bytes, profile: CVImportProfile, error_code: str
) -> None:
    with pytest.raises(CsvParseError) as caught:
        parse_cv_csv(payload, profile)

    assert caught.value.code == error_code


def test_header_location_is_explicit() -> None:
    profile = _profile().model_copy(update={"header_row": 3})

    parsed = parse_cv_csv(b"instrument export\noperator note\nE,I,note\n0,1,x\n", profile)

    assert parsed.row_count == 1


def test_unsupported_unit_mapping_fails_without_coercion() -> None:
    profile = _profile().model_copy(
        update={
            "columns": tuple(
                item.model_copy(update={"source_unit": "kV"}) if item.role == "potential" else item
                for item in _profile().columns
            )
        }
    )

    with pytest.raises(CsvParseError) as caught:
        parse_cv_csv(b"E,I,note\n0,1,x\n", profile)

    assert caught.value.code == "unsupported_unit_mapping"

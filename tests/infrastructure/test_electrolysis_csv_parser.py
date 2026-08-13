from __future__ import annotations

from decimal import Decimal

import pytest

from electrolysis_helpers import ELECTROLYSIS_PAYLOAD, electrolysis_profile
from labbridge.infrastructure.cv_csv import CsvParseError
from labbridge.infrastructure.electrolysis_csv import parse_electrolysis_csv

EXPECTED_ROW_COUNT = 3


def test_parser_uses_only_declared_mapping_and_units() -> None:
    parsed = parse_electrolysis_csv(ELECTROLYSIS_PAYLOAD, electrolysis_profile())

    assert parsed.row_count == EXPECTED_ROW_COUNT
    assert tuple(series.role for series in parsed.series) == ("potential", "current", "time")
    by_role = {series.role: series for series in parsed.series}
    assert by_role["time"].values == (Decimal("0"), Decimal("60"), Decimal("120"))
    assert by_role["current"].values == (
        Decimal("0.0100"),
        Decimal("0.0100"),
        Decimal("0.0100"),
    )
    assert by_role["potential"].target_unit == "V"


def test_parser_fails_closed_on_an_undeclared_column() -> None:
    payload = ELECTROLYSIS_PAYLOAD.replace(
        b"working_potential", b"working_potential,product_yield"
    ).replace(b"-0.420\n", b"-0.420,95\n")

    with pytest.raises(CsvParseError) as raised:
        parse_electrolysis_csv(payload, electrolysis_profile())

    assert raised.value.code == "unexpected_column"

"""Fail-closed CSV parsing driven only by a versioned import profile."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from labbridge.domain.cv import ColumnRole, CSVFormat, CVImportProfile

PARSER_VERSION = "1"


class CsvParseError(ValueError):
    code: str

    def __init__(
        self, code: str, message: str, *, row: int | None = None, column: str | None = None
    ) -> None:
        self.code = code
        self.row = row
        self.column = column
        super().__init__(message)


class UnsupportedUnitMappingError(CsvParseError):
    def __init__(self, source_unit: str, target_unit: str) -> None:
        super().__init__(
            "unsupported_unit_mapping",
            f"no declared conversion from `{source_unit}` to `{target_unit}`",
        )


@dataclass(frozen=True)
class ParsedSeries:
    source_column: str
    role: ColumnRole
    source_unit: str
    target_unit: str
    source_values: tuple[Decimal, ...]
    values: tuple[Decimal, ...]


@dataclass(frozen=True)
class ParsedCV:
    parser_version: str
    headers: tuple[str, ...]
    row_count: int
    series: tuple[ParsedSeries, ...]


@dataclass(frozen=True)
class CSVInspection:
    headers: tuple[str, ...]
    row_count: int


_CONVERSION_FACTORS: dict[tuple[str, str], Decimal] = {
    ("V", "V"): Decimal("1"),
    ("V vs. Ref.", "V"): Decimal("1"),
    ("mV", "V"): Decimal("0.001"),
    ("A", "A"): Decimal("1"),
    ("mA", "A"): Decimal("0.001"),
    ("uA", "A"): Decimal("0.000001"),
    ("µA", "A"): Decimal("0.000001"),
    ("nA", "A"): Decimal("0.000000001"),
    ("A/m^2", "A/m^2"): Decimal("1"),
    ("A/cm^2", "A/m^2"): Decimal("10000"),
    ("mA/cm^2", "A/m^2"): Decimal("10"),
    ("uA/cm^2", "A/m^2"): Decimal("0.01"),
    ("µA/cm^2", "A/m^2"): Decimal("0.01"),
    ("s", "s"): Decimal("1"),
    ("ms", "s"): Decimal("0.001"),
    ("min", "s"): Decimal("60"),
    ("1", "1"): Decimal("1"),
    ("#", "1"): Decimal("1"),
}
_POINT_DECIMAL = re.compile(r"[+-]?\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?")
_COMMA_DECIMAL = re.compile(r"[+-]?\d+(?:,\d+)?(?:[Ee][+-]?\d+)?")


def decimal_syntax_matches(cell: str, convention: Literal["point", "comma"]) -> bool:
    """Return whether one cell uses the bounded declared decimal grammar exactly."""
    pattern = _COMMA_DECIMAL if convention == "comma" else _POINT_DECIMAL
    return pattern.fullmatch(cell) is not None


def _factor(source_unit: str, target_unit: str) -> Decimal:
    try:
        return _CONVERSION_FACTORS[(source_unit, target_unit)]
    except KeyError as error:
        raise UnsupportedUnitMappingError(source_unit, target_unit) from error


def _decimal(cell: str, profile: CVImportProfile, *, row: int, column: str) -> Decimal:
    if cell in profile.missing_value_tokens:
        raise CsvParseError(
            "missing_scientific_value",
            f"row {row} column `{column}` contains a declared missing-value token",
            row=row,
            column=column,
        )
    wrong_separator = (profile.decimal_convention == "point" and "," in cell) or (
        profile.decimal_convention == "comma" and "." in cell
    )
    if wrong_separator:
        raise CsvParseError(
            "locale_mismatch",
            f"row {row} column `{column}` conflicts with the declared decimal convention",
            row=row,
            column=column,
        )
    lexical = cell.replace(",", ".") if profile.decimal_convention == "comma" else cell
    try:
        value = Decimal(lexical)
    except InvalidOperation as error:
        raise CsvParseError(
            "non_numeric_cell",
            f"row {row} column `{column}` is not a decimal number",
            row=row,
            column=column,
        ) from error
    if not value.is_finite():
        raise CsvParseError(
            "non_finite_value",
            f"row {row} column `{column}` is non-finite",
            row=row,
            column=column,
        )
    if not decimal_syntax_matches(cell, profile.decimal_convention):
        raise CsvParseError(
            "non_numeric_cell",
            f"row {row} column `{column}` is not a decimal number",
            row=row,
            column=column,
        )
    return value


def parse_mapped_cv_table(
    *,
    headers: tuple[str, ...],
    rows: tuple[tuple[int, tuple[str, ...]], ...],
    profile: CVImportProfile,
    parser_version: str,
) -> ParsedCV:
    """Apply one explicit CV profile to already delimited rows."""
    if not headers or any(not name for name in headers):
        raise CsvParseError("blank_header", "every source column requires a non-blank header")
    if len(headers) != len(set(headers)):
        raise CsvParseError("duplicate_header", "source header names must be unique")

    mapped = {mapping.source_column for mapping in profile.columns}
    present = set(headers)
    missing = sorted(mapped - present)
    if missing:
        raise CsvParseError(
            "missing_column", f"profile columns absent from source: {', '.join(missing)}"
        )
    unexpected = sorted(present - mapped)
    if unexpected:
        raise CsvParseError(
            "unexpected_column",
            "source columns have no explicit mapped or ignored role: " + ", ".join(unexpected),
        )

    for offset, row in rows:
        if not row:
            raise CsvParseError(
                "blank_row",
                f"row {offset} is blank; rows are never removed silently",
                row=offset,
            )
        if len(row) != len(headers):
            raise CsvParseError(
                "row_length_mismatch",
                f"row {offset} has {len(row)} cells; header has {len(headers)}",
                row=offset,
            )

    index = {name: position for position, name in enumerate(headers)}
    role_order = {"potential": 0, "current": 1, "current_density": 1, "time": 2, "cycle": 3}
    scientific = sorted(
        (item for item in profile.columns if item.role != "ignored"),
        key=lambda item: role_order[item.role],
    )
    series: list[ParsedSeries] = []
    for mapping in scientific:
        assert mapping.source_unit is not None and mapping.target_unit is not None
        factor = _factor(mapping.source_unit, mapping.target_unit)
        source_values = tuple(
            _decimal(
                row[index[mapping.source_column]],
                profile,
                row=offset,
                column=mapping.source_column,
            )
            for offset, row in rows
        )
        series.append(
            ParsedSeries(
                source_column=mapping.source_column,
                role=mapping.role,
                source_unit=mapping.source_unit,
                target_unit=mapping.target_unit,
                source_values=source_values,
                values=tuple(value * factor for value in source_values),
            )
        )
    return ParsedCV(
        parser_version=parser_version,
        headers=headers,
        row_count=len(rows),
        series=tuple(series),
    )


def parse_cv_csv(data: bytes, profile: CVImportProfile) -> ParsedCV:
    """Parse exact bytes without guessing dialect, headers, roles, units, or missing values."""
    try:
        text = data.decode(profile.encoding, errors="strict")
    except UnicodeDecodeError as error:
        raise CsvParseError("encoding_error", f"source is not valid {profile.encoding}") from error
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=profile.delimiter, strict=True)
    try:
        source_rows = list(reader)
    except csv.Error as error:
        error_row = reader.line_num or None
        raise CsvParseError(
            "malformed_csv", "source contains malformed CSV quoting", row=error_row
        ) from error
    if len(source_rows) < profile.header_row:
        raise CsvParseError("header_missing", f"header row {profile.header_row} is absent")
    headers = tuple(source_rows[profile.header_row - 1])
    body = tuple(
        (offset, tuple(row))
        for offset, row in enumerate(
            source_rows[profile.header_row :], start=profile.header_row + 1
        )
    )
    return parse_mapped_cv_table(
        headers=headers,
        rows=body,
        profile=profile,
        parser_version=PARSER_VERSION,
    )


def inspect_csv(data: bytes, csv_format: CSVFormat) -> CSVInspection:
    """Read only declared table structure; header names are never interpreted as roles."""
    try:
        text = data.decode(csv_format.encoding, errors="strict")
    except UnicodeDecodeError as error:
        raise CsvParseError(
            "encoding_error", f"source is not valid {csv_format.encoding}"
        ) from error
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=csv_format.delimiter, strict=True)
    try:
        rows = list(reader)
    except csv.Error as error:
        error_row = reader.line_num or None
        raise CsvParseError(
            "malformed_csv", "source contains malformed CSV quoting", row=error_row
        ) from error
    if len(rows) < csv_format.header_row:
        raise CsvParseError("header_missing", f"header row {csv_format.header_row} is absent")
    headers = tuple(rows[csv_format.header_row - 1])
    if not headers or any(not name for name in headers):
        raise CsvParseError("blank_header", "every source column requires a non-blank header")
    if len(headers) != len(set(headers)):
        raise CsvParseError("duplicate_header", "source header names must be unique")
    body = rows[csv_format.header_row :]
    for offset, row in enumerate(body, start=csv_format.header_row + 1):
        if not row:
            raise CsvParseError(
                "blank_row",
                f"row {offset} is blank; rows are never removed silently",
                row=offset,
            )
        if len(row) != len(headers):
            raise CsvParseError(
                "row_length_mismatch",
                f"row {offset} has {len(row)} cells; header has {len(headers)}",
                row=offset,
            )
    return CSVInspection(headers=headers, row_count=len(body))


__all__ = [
    "PARSER_VERSION",
    "CSVInspection",
    "CsvParseError",
    "ParsedCV",
    "ParsedSeries",
    "UnsupportedUnitMappingError",
    "decimal_syntax_matches",
    "inspect_csv",
    "parse_cv_csv",
    "parse_mapped_cv_table",
]

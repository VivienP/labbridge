"""Strict Gamry DTA cyclic-voltammetry parsing for one documented variant."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import NoReturn

from labbridge.domain.cv import CVImportProfile, import_profile_id
from labbridge.domain.parser_diagnostics import (
    ParsedFieldTrace,
    ParserDiagnostic,
    ParserRecord,
    ParserSourceLocation,
    build_parser_record,
)

from .cv_csv import CsvParseError, ParsedCV, decimal_syntax_matches, parse_mapped_cv_table

PARSER_NAME = "labbridge-gamry-dta"
PARSER_VERSION = "gamry-dta/1"
SUPPORTED_VARIANT = "gamry-dta-cv-framework-7.07-curve-v1"
SUPPORTED_FRAMEWORK_VERSION = Decimal("7.07")
SUPPORTED_HEADERS = (
    "Pt",
    "T",
    "Vf",
    "Im",
    "Vu",
    "Sig",
    "Ach",
    "IERange",
    "Over",
    "Cycle",
    "Temp",
)
SUPPORTED_UNITS = (
    "#",
    "s",
    "V vs. Ref.",
    "A",
    "V",
    "V",
    "V",
    "#",
    "bits",
    "#",
    "deg C",
)
SUPPORT_STATEMENT = (
    "Gamry DTA cyclic-voltammetry files with TAG CV, Framework 7.07, and one "
    "CURVE table using the declared Phase 4 column schema are supported."
)
EXCLUSIONS = (
    "Additional or mixed TABLE objects",
    "Techniques other than cyclic voltammetry",
    "Framework versions other than 7.07",
    "Column or unit layouts outside the declared CURVE schema",
    "Implicit locale, unit, reference-scale, or electrode-area interpretation",
)
TAG_LINE = 2
MIN_OBJECT_FIELDS = 2
QUANT_OBJECT_FIELDS = 3
TABLE_DECLARATION_FIELDS = 3


@dataclass(frozen=True)
class ParsedGamryDTA:
    parsed: ParsedCV
    record: ParserRecord


class GamryDtaParseError(ValueError):
    code: str

    def __init__(self, code: str, message: str, *, record: ParserRecord) -> None:
        self.code = code
        self.record = record
        self.parser_record_id = record.parser_record_id
        super().__init__(message)


def _cells(line: str) -> tuple[str, ...]:
    return tuple(line.split("\t"))


def _preserved_object_types(lines: tuple[str, ...], table_line: int | None) -> tuple[str, ...]:
    stop = len(lines) if table_line is None else table_line - 1
    known = {"", "EXPLAIN", "TAG", "FRAMEWORKVERSION", "CURVE"}
    return tuple(
        sorted(
            {cells[0] for line in lines[:stop] if (cells := _cells(line)) and cells[0] not in known}
        )
    )


def _profile_matches_variant(profile: CVImportProfile) -> bool:
    expected = {
        "Pt": ("ignored", None, None),
        "T": ("time", "s", "s"),
        "Vf": ("potential", "V vs. Ref.", "V"),
        "Im": ("current", "A", "A"),
        "Vu": ("ignored", None, None),
        "Sig": ("ignored", None, None),
        "Ach": ("ignored", None, None),
        "IERange": ("ignored", None, None),
        "Over": ("ignored", None, None),
        "Cycle": ("cycle", "#", "1"),
        "Temp": ("ignored", None, None),
    }
    actual = {
        item.source_column: (item.role, item.source_unit, item.target_unit)
        for item in profile.columns
    }
    return actual == expected


def parse_gamry_dta(  # noqa: PLR0912, PLR0915 - branches reject distinct DTA defects
    data: bytes,
    profile: CVImportProfile,
    *,
    source_artifact_id: str,
) -> ParsedGamryDTA:
    """Parse the supported DTA variant without dialect or scientific-semantic inference."""
    profile_id = import_profile_id(profile)
    lines: tuple[str, ...] = ()
    headers: tuple[str, ...] = ()
    table_line: int | None = None

    def reject(
        code: str,
        message: str,
        *,
        line: int | None = None,
        row_count: int | None = None,
    ) -> NoReturn:
        locations = () if line is None else (ParserSourceLocation(line_start=line, line_end=line),)
        record = build_parser_record(
            source_format="gamry_dta",
            parser_name=PARSER_NAME,
            parser_version=PARSER_VERSION,
            supported_variant=SUPPORTED_VARIANT,
            source_artifact_id=source_artifact_id,
            import_profile_id=profile_id,
            status="rejected",
            headers=headers,
            row_count=row_count,
            fields=(),
            diagnostics=(
                ParserDiagnostic(
                    code=code,
                    severity="error",
                    message=message,
                    locations=locations,
                ),
            ),
            preserved_uninterpreted=_preserved_object_types(lines, table_line),
            support_statement=SUPPORT_STATEMENT,
            exclusions=EXCLUSIONS,
        )
        raise GamryDtaParseError(code, message, record=record)

    try:
        text = data.decode(profile.encoding, errors="strict")
    except UnicodeDecodeError:
        reject("dta_encoding_error", f"source is not valid {profile.encoding}")
    lines = tuple(text.splitlines())
    if not lines or lines[0] != "EXPLAIN":
        reject("dta_unsupported_structure", "line 1 must be the EXPLAIN marker", line=1)
    tag_lines = tuple(
        (number, cells)
        for number, line in enumerate(lines, start=1)
        if (cells := _cells(line)) and cells[0] == "TAG"
    )
    if not tag_lines:
        reject("dta_unsupported_structure", "the TAG object is absent")
    if len(tag_lines) > 1:
        reject(
            "dta_mixed_technique_blocks",
            "exactly one TAG object is required by the bounded CV parser",
            line=tag_lines[1][0],
        )
    tag_line, tag = tag_lines[0]
    if tag_line != TAG_LINE or len(tag) < MIN_OBJECT_FIELDS:
        reject(
            "dta_unsupported_structure",
            "line 2 must declare the TAG object",
            line=TAG_LINE,
        )
    if tag[1] != "CV":
        reject(
            "dta_unsupported_technique",
            f"TAG `{tag[1]}` is not the supported CV technique",
            line=TAG_LINE,
        )

    framework_lines = tuple(
        (number, cells)
        for number, line in enumerate(lines, start=1)
        if (cells := _cells(line)) and cells[0] == "FRAMEWORKVERSION"
    )
    if len(framework_lines) != 1:
        reject(
            "dta_unsupported_structure",
            "exactly one FRAMEWORKVERSION object is required",
        )
    framework_line, framework = framework_lines[0]
    if len(framework) < QUANT_OBJECT_FIELDS or framework[1] != "QUANT":
        reject(
            "dta_unsupported_structure",
            "FRAMEWORKVERSION must contain one QUANT value",
            line=framework_line,
        )
    version_text = framework[2]
    wrong_separator = (profile.decimal_convention == "point" and "," in version_text) or (
        profile.decimal_convention == "comma" and "." in version_text
    )
    if wrong_separator:
        reject(
            "dta_locale_mismatch",
            "FRAMEWORKVERSION decimal syntax conflicts with the import profile",
            line=framework_line,
        )
    if not decimal_syntax_matches(version_text, profile.decimal_convention):
        reject(
            "dta_unsupported_framework_version",
            "FRAMEWORKVERSION is outside the supported decimal grammar",
            line=framework_line,
        )
    try:
        framework_version = Decimal(
            version_text.replace(",", ".")
            if profile.decimal_convention == "comma"
            else version_text
        )
    except InvalidOperation:
        reject(
            "dta_unsupported_framework_version",
            "FRAMEWORKVERSION is not a finite declared decimal",
            line=framework_line,
        )
    if not framework_version.is_finite() or framework_version != SUPPORTED_FRAMEWORK_VERSION:
        reject(
            "dta_unsupported_framework_version",
            f"Framework version `{version_text}` is outside the supported 7.07 variant",
            line=framework_line,
        )

    tables = tuple(
        (number, cells)
        for number, line in enumerate(lines, start=1)
        if len(cells := _cells(line)) >= MIN_OBJECT_FIELDS and cells[1] == "TABLE"
    )
    if len(tables) > 1:
        reject(
            "dta_mixed_technique_blocks",
            "multiple TABLE objects are not accepted by the bounded CV parser",
            line=tables[1][0],
        )
    if not tables:
        reject("dta_unsupported_structure", "the CURVE TABLE object is absent")
    table_line, table = tables[0]
    if len(table) != TABLE_DECLARATION_FIELDS or table[0] != "CURVE":
        reject(
            "dta_unsupported_technique",
            "the only TABLE object must be CURVE with a declared row count",
            line=table_line,
        )
    if re.fullmatch(r"[0-9]+", table[2]) is None:
        reject("dta_invalid_row_count", "CURVE row count is not an integer", line=table_line)
    try:
        declared_rows = int(table[2])
    except ValueError:
        reject("dta_invalid_row_count", "CURVE row count is not an integer", line=table_line)
    if declared_rows < 1:
        reject("dta_invalid_row_count", "CURVE row count must be positive", line=table_line)

    header_line = table_line + 1
    unit_line = table_line + 2
    data_start_line = table_line + 3
    if len(lines) < unit_line:
        reject(
            "dta_table_truncated",
            "CURVE header or unit row is absent",
            line=table_line,
            row_count=0,
        )
    header_cells = _cells(lines[header_line - 1])
    unit_cells = _cells(lines[unit_line - 1])
    headers = header_cells[1:] if header_cells[:1] == ("",) else header_cells
    units = unit_cells[1:] if unit_cells[:1] == ("",) else unit_cells
    if header_cells[:1] != ("",):
        reject(
            "dta_unsupported_table_schema",
            "the CURVE header row must retain the leading DTA table field",
            line=header_line,
        )
    if unit_cells[:1] != ("",):
        reject(
            "dta_unsupported_table_schema",
            "the CURVE unit row must retain the leading DTA table field",
            line=unit_line,
        )
    if headers != SUPPORTED_HEADERS:
        reject(
            "dta_unsupported_table_schema",
            "CURVE headers do not match the declared supported schema",
            line=header_line,
        )
    if units != SUPPORTED_UNITS:
        reject(
            "dta_unsupported_table_schema",
            "CURVE units do not match the declared supported schema",
            line=unit_line,
        )
    if profile.delimiter != "\t" or profile.header_row != header_line:
        reject(
            "dta_profile_mismatch",
            "import profile delimiter or header row does not match the CURVE table",
            line=header_line,
        )
    if not _profile_matches_variant(profile):
        reject(
            "dta_profile_mismatch",
            "import profile roles and units do not match the supported CURVE mapping",
            line=header_line,
        )

    available_rows = max(0, len(lines) - (data_start_line - 1))
    if available_rows < declared_rows:
        reject(
            "dta_table_truncated",
            f"CURVE declares {declared_rows} rows but only {available_rows} are present",
            line=max(table_line, len(lines)),
            row_count=available_rows,
        )
    if available_rows > declared_rows:
        reject(
            "dta_unsupported_trailing_content",
            "content follows the declared CURVE rows",
            line=data_start_line + declared_rows,
            row_count=declared_rows,
        )

    rows = tuple(
        (
            line_number,
            cells[1:] if cells[:1] == ("",) else cells,
        )
        for line_number in range(data_start_line, data_start_line + declared_rows)
        for cells in (_cells(lines[line_number - 1]),)
    )
    if any(_cells(lines[line_number - 1])[:1] != ("",) for line_number, _ in rows):
        reject(
            "dta_unsupported_table_schema",
            "each CURVE row must retain the leading DTA table field",
            line=data_start_line,
            row_count=declared_rows,
        )
    try:
        parsed = parse_mapped_cv_table(
            headers=headers,
            rows=rows,
            profile=profile,
            parser_version=PARSER_VERSION,
        )
    except CsvParseError as error:
        code = f"dta_{error.code}"
        reject(
            code,
            str(error),
            line=error.row,
            row_count=declared_rows,
        )

    data_end_line = data_start_line + declared_rows - 1
    scientific = {item.source_column: item for item in profile.columns if item.role != "ignored"}
    fields = tuple(
        ParsedFieldTrace(
            source_column=series.source_column,
            role=series.role,
            source_unit=series.source_unit,
            header_line=header_line,
            unit_line=unit_line,
            data_start_line=data_start_line,
            data_end_line=data_end_line,
        )
        for series in parsed.series
        if series.source_column in scientific
    )
    record = build_parser_record(
        source_format="gamry_dta",
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
        supported_variant=SUPPORTED_VARIANT,
        source_artifact_id=source_artifact_id,
        import_profile_id=profile_id,
        status="accepted",
        headers=headers,
        row_count=parsed.row_count,
        fields=fields,
        diagnostics=(
            ParserDiagnostic(
                code="dta_supported_variant",
                severity="info",
                message="the source matches the bounded Gamry DTA CV variant",
                locations=(
                    ParserSourceLocation(
                        line_start=table_line,
                        line_end=data_end_line,
                        object_type="CURVE",
                        object_tag="TABLE",
                    ),
                ),
            ),
        ),
        preserved_uninterpreted=_preserved_object_types(lines, table_line),
        support_statement=SUPPORT_STATEMENT,
        exclusions=EXCLUSIONS,
    )
    return ParsedGamryDTA(parsed=parsed, record=record)


__all__ = [
    "EXCLUSIONS",
    "PARSER_NAME",
    "PARSER_VERSION",
    "SUPPORTED_HEADERS",
    "SUPPORTED_UNITS",
    "SUPPORTED_VARIANT",
    "GamryDtaParseError",
    "ParsedGamryDTA",
    "parse_gamry_dta",
]

from __future__ import annotations

from decimal import Decimal

import pytest

from labbridge.domain.cv import ColumnMapping, CVImportProfile, CVMetadata, import_profile_id
from labbridge.infrastructure.gamry_dta import GamryDtaParseError, parse_gamry_dta

SOURCE_ID = "source-artifact:synthetic-gamry"
HEADERS = "\tPt\tT\tVf\tIm\tVu\tSig\tAch\tIERange\tOver\tCycle\tTemp"
UNITS = "\t#\ts\tV vs. Ref.\tA\tV\tV\tV\t#\tbits\t#\tdeg C"
EXPECTED_ROW_COUNT = 2


def _profile(
    *,
    decimal_convention: str = "point",
    encoding: str = "utf-8",
    header_row: int = 7,
) -> CVImportProfile:
    return CVImportProfile(
        schema_version="1",
        technique="cyclic_voltammetry",
        environment_id="synthetic_gamry_cv_fixture",
        encoding=encoding,
        delimiter="\t",
        decimal_convention=decimal_convention,
        header_row=header_row,
        missing_value_tokens=("",),
        columns=(
            ColumnMapping(source_column="Pt", role="ignored"),
            ColumnMapping(source_column="T", role="time", source_unit="s", target_unit="s"),
            ColumnMapping(
                source_column="Vf",
                role="potential",
                source_unit="V vs. Ref.",
                target_unit="V",
            ),
            ColumnMapping(source_column="Im", role="current", source_unit="A", target_unit="A"),
            ColumnMapping(source_column="Vu", role="ignored"),
            ColumnMapping(source_column="Sig", role="ignored"),
            ColumnMapping(source_column="Ach", role="ignored"),
            ColumnMapping(source_column="IERange", role="ignored"),
            ColumnMapping(source_column="Over", role="ignored"),
            ColumnMapping(source_column="Cycle", role="cycle", source_unit="#", target_unit="1"),
            ColumnMapping(source_column="Temp", role="ignored"),
        ),
        metadata=CVMetadata.unknown(),
    )


def _payload(
    *,
    tag: str = "CV",
    framework_version: str = "7.07",
    count: int | str = 2,
    headers: str = HEADERS,
    units: str = UNITS,
    rows: tuple[str, ...] = (
        "\t0\t0.00\t-2.40000E-001\t1.20000E-002\t0\t-0.24\t0\t9\t..........\t0\t25.0",
        "\t1\t0.10\t1.20000E-001\t-3.10000E-002\t0\t0.12\t0\t9\t..........\t1\t25.0",
    ),
    extra_objects: tuple[str, ...] = (),
) -> bytes:
    lines = (
        "EXPLAIN",
        f"TAG\t{tag}",
        "TITLE\tLABEL\tSynthetic Cyclic Voltammetry\tTest Identifier",
        f"FRAMEWORKVERSION\tQUANT\t{framework_version}\tFramework Version",
        "NOTES\tNOTES\t0\tSynthetic redistributable fixture",
        *extra_objects,
        f"CURVE\tTABLE\t{count}",
        headers,
        units,
        *rows,
    )
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def test_supported_framework_707_cv_table_has_exact_field_locations() -> None:
    profile = _profile()

    result = parse_gamry_dta(_payload(), profile, source_artifact_id=SOURCE_ID)

    assert result.parsed.parser_version == "gamry-dta/1"
    assert result.parsed.row_count == EXPECTED_ROW_COUNT
    assert [item.role for item in result.parsed.series] == [
        "potential",
        "current",
        "time",
        "cycle",
    ]
    assert result.parsed.series[0].values == (Decimal("-0.240000"), Decimal("0.120000"))
    assert result.parsed.series[1].values == (Decimal("0.012000"), Decimal("-0.031000"))
    assert result.record.status == "accepted"
    assert result.record.import_profile_id == import_profile_id(profile)
    assert result.record.row_count == EXPECTED_ROW_COUNT
    traces = {item.source_column: item for item in result.record.fields}
    assert (traces["Vf"].header_line, traces["Vf"].unit_line) == (7, 8)
    assert (traces["Vf"].data_start_line, traces["Vf"].data_end_line) == (9, 10)
    assert result.record.preserved_uninterpreted == ("NOTES", "TITLE")


@pytest.mark.parametrize(
    "payload,profile,error_code",
    [
        (_payload(count=3), _profile(), "dta_table_truncated"),
        (_payload(count="0_2"), _profile(), "dta_invalid_row_count"),
        (
            _payload(extra_objects=("ZCURVE\tTABLE\t1",)),
            _profile(),
            "dta_mixed_technique_blocks",
        ),
        (
            _payload(extra_objects=("TAG\tEIS",)),
            _profile(header_row=8),
            "dta_mixed_technique_blocks",
        ),
        (_payload(tag="EIS"), _profile(), "dta_unsupported_technique"),
        (_payload(framework_version="7.08"), _profile(), "dta_unsupported_framework_version"),
        (
            _payload(framework_version=" 7.07 "),
            _profile(),
            "dta_unsupported_framework_version",
        ),
        (
            _payload(headers=HEADERS.replace("\tIm\t", "\tZreal\t")),
            _profile(),
            "dta_unsupported_table_schema",
        ),
        (
            _payload(headers=HEADERS.replace("\tIm\t", "\tVf\t")),
            _profile(),
            "dta_unsupported_table_schema",
        ),
        (
            _payload(headers=HEADERS.removeprefix("\t")),
            _profile(),
            "dta_unsupported_table_schema",
        ),
        (
            _payload(units=UNITS.removeprefix("\t")),
            _profile(),
            "dta_unsupported_table_schema",
        ),
        (
            _payload(rows=("\t0\t0.00\t-0.24\t0.012",)),
            _profile(),
            "dta_table_truncated",
        ),
        (
            _payload(
                rows=(
                    "\t0\t0.00\t-0.24\t0.012",
                    "\t1\t0.10\t0.12\t-0.031\t0\t0.12\t0\t9\t..........\t1\t25.0",
                )
            ),
            _profile(),
            "dta_row_length_mismatch",
        ),
        (
            _payload(
                rows=(
                    "0\t0.00\t-2.40000E-001\t1.20000E-002\t0\t-0.24\t0\t9\t..........\t0\t25.0",
                    "\t1\t0.10\t1.20000E-001\t-3.10000E-002\t0\t0.12\t0\t9\t..........\t1\t25.0",
                )
            ),
            _profile(),
            "dta_unsupported_table_schema",
        ),
        (
            _payload(
                rows=(
                    "\t0\t0.00\t\t1.20000E-002\t0\t-0.24\t0\t9\t..........\t0\t25.0",
                    "\t1\t0.10\t1.20000E-001\t-3.10000E-002\t0\t0.12\t0\t9\t..........\t1\t25.0",
                )
            ),
            _profile(),
            "dta_missing_scientific_value",
        ),
        (
            _payload(
                rows=(
                    "\t0\t0.00\tinvalid\t1.20000E-002\t0\t-0.24\t0\t9\t..........\t0\t25.0",
                    "\t1\t0.10\t1.20000E-001\t-3.10000E-002\t0\t0.12\t0\t9\t..........\t1\t25.0",
                )
            ),
            _profile(),
            "dta_non_numeric_cell",
        ),
        (
            _payload(
                rows=(
                    "\t0\t0.00\t-2.40000E-001\t1_2\t0\t-0.24\t0\t9\t..........\t0\t25.0",
                    "\t1\t0.10\t1.20000E-001\t-3.10000E-002\t0\t0.12\t0\t9\t..........\t1\t25.0",
                )
            ),
            _profile(),
            "dta_non_numeric_cell",
        ),
        (
            _payload(
                rows=(
                    "\t0\t0.00\tNaN\t1.20000E-002\t0\t-0.24\t0\t9\t..........\t0\t25.0",
                    "\t1\t0.10\t1.20000E-001\t-3.10000E-002\t0\t0.12\t0\t9\t..........\t1\t25.0",
                )
            ),
            _profile(),
            "dta_non_finite_value",
        ),
        (_payload(framework_version="7,07"), _profile(), "dta_locale_mismatch"),
        (
            _payload(framework_version="7,07"),
            _profile(decimal_convention="comma"),
            "dta_locale_mismatch",
        ),
    ],
)
def test_unsupported_or_ambiguous_dta_fails_with_a_retained_diagnostic(
    payload: bytes, profile: CVImportProfile, error_code: str
) -> None:
    with pytest.raises(GamryDtaParseError) as caught:
        parse_gamry_dta(payload, profile, source_artifact_id=SOURCE_ID)

    assert caught.value.code == error_code
    assert caught.value.record.status == "rejected"
    assert caught.value.record.source_artifact_id == SOURCE_ID
    assert caught.value.record.diagnostics[-1].code == error_code
    assert caught.value.parser_record_id == caught.value.record.parser_record_id


def test_declared_decimal_comma_is_supported_without_detection() -> None:
    profile = _profile(decimal_convention="comma")
    payload = _payload(
        framework_version="7,07",
        rows=(
            "\t0\t0,00\t-2,40000E-001\t1,20000E-002\t0\t-0,24\t0\t9\t..........\t0\t25,0",
            "\t1\t0,10\t1,20000E-001\t-3,10000E-002\t0\t0,12\t0\t9\t..........\t1\t25,0",
        ),
    )

    result = parse_gamry_dta(payload, profile, source_artifact_id=SOURCE_ID)

    assert result.parsed.series[0].values == (Decimal("-0.240000"), Decimal("0.120000"))


def test_invalid_encoding_fails_before_structural_interpretation() -> None:
    with pytest.raises(GamryDtaParseError) as caught:
        parse_gamry_dta(b"EXPLAIN\r\nTAG\tCV\r\n\xff", _profile(), source_artifact_id=SOURCE_ID)

    assert caught.value.code == "dta_encoding_error"
    assert caught.value.record.headers == ()


def test_utf8_bom_is_supported_only_when_the_profile_declares_it() -> None:
    payload = b"\xef\xbb\xbf" + _payload()

    result = parse_gamry_dta(
        payload,
        _profile(encoding="utf-8-sig"),
        source_artifact_id=SOURCE_ID,
    )

    assert result.record.status == "accepted"

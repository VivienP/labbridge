from __future__ import annotations

from labbridge.domain.parser_diagnostics import (
    ParsedFieldTrace,
    ParserDiagnostic,
    ParserSourceLocation,
    build_parser_record,
    parser_record_id,
)

EXPECTED_DATA_START_LINE = 10


def _record(*, header_line: int = 8):
    return build_parser_record(
        source_format="gamry_dta",
        parser_name="labbridge.infrastructure.gamry_dta.parse_gamry_dta",
        parser_version="gamry-dta/1",
        supported_variant="gamry-dta-cv-framework-7.07-curve-v1",
        source_artifact_id="source-artifact:abc",
        import_profile_id="cv-profile:def",
        status="accepted",
        headers=("Pt", "T", "Vf", "Im"),
        row_count=2,
        fields=(
            ParsedFieldTrace(
                source_column="Vf",
                role="potential",
                source_unit="V vs. Ref.",
                header_line=header_line,
                unit_line=header_line + 1,
                data_start_line=header_line + 2,
                data_end_line=header_line + 3,
            ),
        ),
        diagnostics=(
            ParserDiagnostic(
                code="gamry.structure.supported",
                severity="info",
                message="The declared Gamry DTA CV structure is supported.",
                locations=(
                    ParserSourceLocation(
                        line_start=1,
                        line_end=header_line + 3,
                        object_type="CURVE",
                        object_tag="TABLE",
                    ),
                ),
            ),
        ),
        preserved_uninterpreted=("TITLE", "NOTES"),
        support_statement="One explicit text DTA CV variant is supported.",
        exclusions=("multiple TABLE objects", "non-CV techniques"),
    )


def test_parser_record_identity_covers_field_locations_and_recomputes() -> None:
    first = _record()
    moved = _record(header_line=9)

    assert first.parser_record_id == parser_record_id(first)
    assert first.parser_record_id != moved.parser_record_id
    assert first.fields[0].data_start_line == EXPECTED_DATA_START_LINE


def test_rejected_parser_record_cannot_claim_accepted_fields() -> None:
    accepted = _record()

    try:
        accepted.model_copy(update={"status": "rejected"}).model_validate(
            accepted.model_copy(update={"status": "rejected"}).model_dump()
        )
    except ValueError as error:
        assert "rejected parser record cannot contain accepted fields" in str(error)
    else:
        raise AssertionError("rejected parser record accepted field traces")

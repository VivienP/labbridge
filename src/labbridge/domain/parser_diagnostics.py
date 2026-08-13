"""Content-addressed parser diagnostics rooted in one retained source artifact."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical import content_id
from .cv import ColumnRole

SourceFormat = Literal["generic_csv", "gamry_dta"]
ParserStatus = Literal["accepted", "rejected"]
DiagnosticSeverity = Literal["info", "warning", "error"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ParserSourceLocation(_Model):
    """One one-based source span retained with a parser diagnostic."""

    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    object_type: str | None = None
    object_tag: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.line_end < self.line_start:
            raise ValueError("parser source location ends before it starts")
        return self


class ParsedFieldTrace(_Model):
    """Exact DTA lines supporting one field accepted by the shared CV mapping."""

    source_column: str = Field(min_length=1)
    role: ColumnRole
    source_unit: str = Field(min_length=1)
    header_line: int = Field(ge=1)
    unit_line: int = Field(ge=1)
    data_start_line: int = Field(ge=1)
    data_end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not (self.header_line < self.unit_line < self.data_start_line <= self.data_end_line):
            raise ValueError("parsed field trace lines are not ordered")
        return self


class ParserDiagnostic(_Model):
    code: str = Field(min_length=1)
    severity: DiagnosticSeverity
    message: str = Field(min_length=1)
    locations: tuple[ParserSourceLocation, ...] = ()


class ParserRecord(_Model):
    """Immutable parser result retained for accepted and rejected source interpretations."""

    parser_record_id: str = Field(min_length=1)
    schema_version: Literal["1"]
    source_format: SourceFormat
    parser_name: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)
    supported_variant: str = Field(min_length=1)
    source_artifact_id: str = Field(min_length=1)
    import_profile_id: str = Field(min_length=1)
    status: ParserStatus
    headers: tuple[str, ...] = ()
    row_count: int | None = Field(default=None, ge=0)
    fields: tuple[ParsedFieldTrace, ...] = ()
    diagnostics: tuple[ParserDiagnostic, ...] = Field(min_length=1)
    preserved_uninterpreted: tuple[str, ...] = ()
    support_statement: str = Field(min_length=1)
    exclusions: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _status_matches_content(self) -> Self:
        if self.status == "accepted":
            if self.row_count is None or self.row_count < 1:
                raise ValueError("accepted parser record requires parsed rows")
            if not self.fields:
                raise ValueError("accepted parser record requires field traces")
        else:
            if self.fields:
                raise ValueError("rejected parser record cannot contain accepted fields")
            if not any(item.severity == "error" for item in self.diagnostics):
                raise ValueError("rejected parser record requires an error diagnostic")
        if self.status == "accepted" and len(self.headers) != len(set(self.headers)):
            raise ValueError("parser record headers must be unique")
        if self.parser_record_id != parser_record_id(self):
            raise ValueError("parser record identity does not match its canonical content")
        return self


def _record_body(record: ParserRecord) -> dict[str, object]:
    return record.model_dump(mode="python", exclude={"parser_record_id"})


def parser_record_id(record: ParserRecord) -> str:
    """Recompute the identity of a retained parser record."""
    return content_id("parser-record", _record_body(record))


def build_parser_record(
    *,
    source_format: SourceFormat,
    parser_name: str,
    parser_version: str,
    supported_variant: str,
    source_artifact_id: str,
    import_profile_id: str,
    status: ParserStatus,
    headers: tuple[str, ...],
    row_count: int | None,
    fields: tuple[ParsedFieldTrace, ...],
    diagnostics: tuple[ParserDiagnostic, ...],
    preserved_uninterpreted: tuple[str, ...],
    support_statement: str,
    exclusions: tuple[str, ...],
) -> ParserRecord:
    """Build a parser record whose identity covers diagnostics and source locations."""
    body = {
        "schema_version": "1",
        "source_format": source_format,
        "parser_name": parser_name,
        "parser_version": parser_version,
        "supported_variant": supported_variant,
        "source_artifact_id": source_artifact_id,
        "import_profile_id": import_profile_id,
        "status": status,
        "headers": headers,
        "row_count": row_count,
        "fields": fields,
        "diagnostics": diagnostics,
        "preserved_uninterpreted": preserved_uninterpreted,
        "support_statement": support_statement,
        "exclusions": exclusions,
    }
    return ParserRecord(
        parser_record_id=content_id("parser-record", body),
        **body,
    )


__all__ = [
    "DiagnosticSeverity",
    "ParsedFieldTrace",
    "ParserDiagnostic",
    "ParserRecord",
    "ParserSourceLocation",
    "ParserStatus",
    "SourceFormat",
    "build_parser_record",
    "parser_record_id",
]

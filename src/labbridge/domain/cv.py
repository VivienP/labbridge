"""Explicit contracts for generic cyclic-voltammetry CSV ingestion.

The profile is the only authority for CSV syntax, column roles, units, and metadata states. Neither
filenames nor header text assign scientific meaning. Profiles and normalised observations are
immutable, versioned, and content-addressed so a changed mapping produces a distinct record while
retaining the same source artifact.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical import content_id

MetadataState = Literal["known", "unknown", "unavailable", "not_applicable"]
ColumnRole = Literal["potential", "current", "current_density", "time", "cycle", "ignored"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MetadataValue(_Model):
    """One metadata value whose evidence state is independent from its value."""

    state: MetadataState
    value: Decimal | str | None = None
    unit: str | None = None

    @model_validator(mode="after")
    def _state_matches_value(self) -> Self:
        if self.state == "known" and self.value is None:
            raise ValueError("known metadata requires a value")
        if self.state != "known" and (self.value is not None or self.unit is not None):
            raise ValueError(f"{self.state} metadata carries no value or unit")
        if self.unit is not None and not self.unit:
            raise ValueError("metadata unit cannot be blank")
        if self.state == "known" and isinstance(self.value, Decimal) and self.unit is None:
            raise ValueError("known numeric metadata requires an explicit unit")
        return self


class CVMetadata(_Model):
    """Phase 2 states required to avoid inventing electrochemical context."""

    reference_scale: MetadataValue
    potential_treatment: MetadataValue
    current_basis: MetadataValue
    electrode_role: MetadataValue
    geometric_area: MetadataValue
    contact_area: MetadataValue
    scan_rate: MetadataValue
    cycle_information: MetadataValue

    @classmethod
    def unknown(cls) -> CVMetadata:
        unknown = MetadataValue(state="unknown")
        return cls(
            reference_scale=unknown,
            potential_treatment=unknown,
            current_basis=unknown,
            electrode_role=unknown,
            geometric_area=unknown,
            contact_area=unknown,
            scan_rate=unknown,
            cycle_information=unknown,
        )


class ColumnMapping(_Model):
    """One explicit decision about one source column."""

    source_column: str = Field(min_length=1)
    role: ColumnRole
    source_unit: str | None = None
    target_unit: str | None = None

    @model_validator(mode="after")
    def _units_match_role(self) -> Self:
        if self.role == "ignored":
            if self.source_unit is not None or self.target_unit is not None:
                raise ValueError("an ignored column must not carry scientific units")
            return self
        if not self.source_unit or not self.target_unit:
            raise ValueError("a scientific mapping requires source_unit and target_unit")
        return self


class CSVFormat(_Model):
    """Only the syntax required to inspect a source table without assigning column roles."""

    encoding: Literal["utf-8", "utf-8-sig"]
    delimiter: str = Field(min_length=1, max_length=1)
    header_row: int = Field(ge=1)

    @model_validator(mode="after")
    def _delimiter_is_lexical(self) -> Self:
        if self.delimiter in {"\r", "\n", '"'}:
            raise ValueError("delimiter must be one non-newline, non-quote character")
        return self


class CVImportProfile(_Model):
    """Versioned, complete and immutable instructions for parsing one CSV shape."""

    schema_version: Literal["1"]
    technique: Literal["cyclic_voltammetry"]
    environment_id: str = Field(min_length=1)
    encoding: Literal["utf-8", "utf-8-sig"]
    delimiter: str = Field(min_length=1, max_length=1)
    decimal_convention: Literal["point", "comma"]
    header_row: int = Field(ge=1)
    missing_value_tokens: tuple[str, ...]
    columns: tuple[ColumnMapping, ...] = Field(min_length=1)
    metadata: CVMetadata

    @model_validator(mode="after")
    def _profile_is_complete(self) -> Self:
        if self.delimiter in {"\r", "\n", '"'}:
            raise ValueError("delimiter must be one non-newline, non-quote character")
        if self.decimal_convention == "comma" and self.delimiter == ",":
            raise ValueError("decimal comma requires a delimiter other than comma")
        names = [mapping.source_column for mapping in self.columns]
        if len(names) != len(set(names)):
            raise ValueError("each source column must appear exactly once in the import profile")
        if len(self.missing_value_tokens) != len(set(self.missing_value_tokens)):
            raise ValueError("missing-value tokens must be unique")
        roles = [mapping.role for mapping in self.columns]
        if roles.count("potential") != 1:
            raise ValueError("a CV profile requires exactly one potential column")
        current_axes = roles.count("current") + roles.count("current_density")
        if current_axes != 1:
            raise ValueError("a CV profile requires exactly one current or current_density column")
        if roles.count("time") > 1 or roles.count("cycle") > 1:
            raise ValueError("time and cycle roles may each be mapped at most once")
        return self


def _profile_payload(profile: CVImportProfile) -> dict[str, object]:
    payload = profile.model_dump(mode="python")
    payload["columns"] = [
        mapping.model_dump(mode="python")
        for mapping in sorted(profile.columns, key=lambda item: item.source_column)
    ]
    payload["missing_value_tokens"] = sorted(profile.missing_value_tokens)
    return payload


def import_profile_id(profile: CVImportProfile) -> str:
    """Identify all parsing, mapping, unit, and metadata decisions canonically."""
    return content_id("cv-profile", _profile_payload(profile))


__all__ = [
    "CSVFormat",
    "CVImportProfile",
    "CVMetadata",
    "ColumnMapping",
    "ColumnRole",
    "MetadataState",
    "MetadataValue",
    "import_profile_id",
]

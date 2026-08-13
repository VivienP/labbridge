"""Explicit profile for bounded generic galvanostatic-electrolysis CSV ingestion."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical import content_id
from .cv import MetadataState, MetadataValue

ElectrolysisColumnRole = Literal["time", "current", "current_density", "potential", "ignored"]

_ROLE_UNITS: dict[ElectrolysisColumnRole, frozenset[str]] = {
    "time": frozenset({"ms", "s"}),
    "current": frozenset({"mA", "A"}),
    "current_density": frozenset({"mA/cm^2", "A/m^2"}),
    "potential": frozenset({"mV", "V"}),
    "ignored": frozenset(),
}
_KNOWN_METADATA_VALUES: dict[str, frozenset[str]] = {
    "current_sign_convention": frozenset(
        {"anodic_positive", "cathodic_positive", "unsigned_magnitude"}
    ),
    "current_basis": frozenset(
        {
            "total_current",
            "geometric_area",
            "electrochemically_active_area",
            "contact_or_wetted_area",
        }
    ),
    "cell_geometry": frozenset({"two_electrode", "three_electrode"}),
    "reference_scale": frozenset({"rhe", "she", "nhe", "ag_agcl", "sce"}),
    "potential_treatment": frozenset({"as_recorded_no_correction_claim"}),
    "interruptions": frozenset({"none_declared"}),
    "chemical_analysis": frozenset({"source_linked_results_declared"}),
}
_AREA_BASES = _KNOWN_METADATA_VALUES["current_basis"] - {"total_current"}


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ElectrolysisMetadata(_Model):
    """Declared electrical context without product-quantification inference."""

    current_sign_convention: MetadataValue
    current_basis: MetadataValue
    electrode_area: MetadataValue
    cell_geometry: MetadataValue
    reference_scale: MetadataValue
    potential_treatment: MetadataValue
    sampling_interval: MetadataValue
    interruptions: MetadataValue
    chemical_analysis: MetadataValue

    @model_validator(mode="before")
    @classmethod
    def _normalise_numeric_metadata(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        normalised = dict(data)
        for field_name in ("electrode_area", "sampling_interval"):
            raw = normalised.get(field_name)
            if (
                isinstance(raw, dict)
                and raw.get("unit") is not None
                and isinstance(raw.get("value"), str)
            ):
                normalised[field_name] = {**raw, "value": Decimal(raw["value"])}
        return normalised

    @model_validator(mode="after")
    def _known_values_are_controlled(self) -> Self:
        for field_name, allowed in _KNOWN_METADATA_VALUES.items():
            item = getattr(self, field_name)
            if item.state == "known" and item.value not in allowed:
                raise ValueError(f"{field_name} has no supported controlled value")
        if self.electrode_area.state == "known":
            if not isinstance(self.electrode_area.value, Decimal):
                raise ValueError("electrode_area requires a numeric value")
            if self.electrode_area.value <= 0 or self.electrode_area.unit not in {"m^2", "cm^2"}:
                raise ValueError("electrode_area requires a positive supported area unit")
        if self.sampling_interval.state == "known":
            if not isinstance(self.sampling_interval.value, Decimal):
                raise ValueError("sampling_interval requires a numeric value")
            if self.sampling_interval.value <= 0 or self.sampling_interval.unit not in {"ms", "s"}:
                raise ValueError("sampling_interval requires a positive supported time unit")
        return self


class ElectrolysisColumnMapping(_Model):
    """One explicit electrolysis source-column role and unit conversion."""

    source_column: str = Field(min_length=1)
    role: ElectrolysisColumnRole
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
        allowed = _ROLE_UNITS[self.role]
        if self.source_unit not in allowed or self.target_unit not in allowed:
            raise ValueError("units are incompatible with electrolysis role")
        return self


class AuxiliaryAnalyticalResult(_Model):
    """A declared analytical result linked to retained electrical and analytical sources."""

    result_id: str = Field(min_length=1)
    electrical_source_artifact_id: str = Field(min_length=1)
    source_artifact_id: str = Field(min_length=1)
    method_name: str = Field(min_length=1)
    method_version: str = Field(min_length=1)
    source_location: str = Field(min_length=1)
    sample_id: str = Field(min_length=1)
    collection_point: str = Field(min_length=1)
    analyte: str = Field(min_length=1)
    quantity_kind: Literal["concentration"]
    value: Decimal
    unit: str = Field(min_length=1)


def _validate_current_context(
    profile: ElectrolysisImportProfile, roles: list[ElectrolysisColumnRole]
) -> None:
    if "current" in roles:
        if profile.metadata.current_basis.value != "total_current":
            raise ValueError("total current requires current_basis=total_current")
        if profile.metadata.electrode_area.state != "not_applicable":
            raise ValueError("total current requires electrode_area=not_applicable")
    if "current_density" in roles and (
        profile.metadata.current_basis.state != "known"
        or profile.metadata.current_basis.value not in _AREA_BASES
        or profile.metadata.electrode_area.state != "known"
    ):
        raise ValueError("current density requires a known area basis and electrode area")


def _validate_auxiliary_context(profile: ElectrolysisImportProfile) -> None:
    if profile.auxiliary_results and (
        profile.metadata.chemical_analysis.state != "known"
        or profile.metadata.chemical_analysis.value != "source_linked_results_declared"
    ):
        raise ValueError(
            "auxiliary results require chemical_analysis=source_linked_results_declared"
        )
    if not profile.auxiliary_results and profile.metadata.chemical_analysis.state == "known":
        raise ValueError("known chemical_analysis requires an auxiliary result inventory")


def auxiliary_result_id(result: AuxiliaryAnalyticalResult) -> str:
    body = result.model_dump(mode="python", exclude={"result_id"})
    return content_id("electrolysis-auxiliary-result", body)


class ElectrolysisImportProfile(_Model):
    """Complete instructions for one galvanostatic-electrolysis CSV shape."""

    schema_version: Literal["1"]
    technique: Literal["galvanostatic_electrolysis"]
    environment_id: str = Field(min_length=1)
    encoding: Literal["utf-8", "utf-8-sig"]
    delimiter: str = Field(min_length=1, max_length=1)
    decimal_convention: Literal["point", "comma"]
    header_row: int = Field(ge=1)
    missing_value_tokens: tuple[str, ...]
    columns: tuple[ElectrolysisColumnMapping, ...] = Field(min_length=1)
    metadata: ElectrolysisMetadata
    auxiliary_results: tuple[AuxiliaryAnalyticalResult, ...]

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
        if roles.count("time") != 1:
            raise ValueError("an electrolysis profile requires exactly one time column")
        if roles.count("potential") != 1:
            raise ValueError("an electrolysis profile requires exactly one potential column")
        if roles.count("current") + roles.count("current_density") != 1:
            raise ValueError(
                "an electrolysis profile requires exactly one current or current_density column"
            )
        _validate_current_context(self, roles)
        _validate_auxiliary_context(self)
        result_ids = [result.result_id for result in self.auxiliary_results]
        if len(result_ids) != len(set(result_ids)):
            raise ValueError("auxiliary analytical result identities must be unique")
        if any(
            result.result_id != auxiliary_result_id(result) for result in self.auxiliary_results
        ):
            raise ValueError("auxiliary analytical result identity does not match its content")
        return self


def electrolysis_import_profile_id(profile: ElectrolysisImportProfile) -> str:
    payload = profile.model_dump(mode="python")
    payload["columns"] = [
        mapping.model_dump(mode="python")
        for mapping in sorted(profile.columns, key=lambda item: item.source_column)
    ]
    payload["missing_value_tokens"] = sorted(profile.missing_value_tokens)
    payload["auxiliary_results"] = [
        result.model_dump(mode="python")
        for result in sorted(profile.auxiliary_results, key=lambda item: item.result_id)
    ]
    return content_id("electrolysis-profile", payload)


__all__ = [
    "AuxiliaryAnalyticalResult",
    "ElectrolysisColumnMapping",
    "ElectrolysisColumnRole",
    "ElectrolysisImportProfile",
    "ElectrolysisMetadata",
    "MetadataState",
    "MetadataValue",
    "auxiliary_result_id",
    "electrolysis_import_profile_id",
]

"""Normalised CV observations and their closed transformation graph."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical import content_id
from .cv import ColumnRole, CVMetadata
from .identity import DataOrigin, ExecutionMode

OBSERVATION_SCHEMA_VERSION = "1"
SERIES_SCHEMA_VERSION = "1"
SERIES_DTYPE = "decimal"
TRANSFORMATION_SCHEMA_VERSION = "1"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TransformationParameter(_Model):
    name: str = Field(min_length=1)
    value: str


class TransformationRecord(_Model):
    transformation_id: str = Field(min_length=1)
    schema_version: Literal["1"]
    kind: Literal["csv_parse", "column_mapping", "observation_assembly"]
    implementation: str = Field(min_length=1)
    implementation_version: str = Field(min_length=1)
    input_ids: tuple[str, ...] = Field(min_length=1)
    parameters: tuple[TransformationParameter, ...]
    output_ids: tuple[str, ...] = Field(min_length=1)


def _transformation_record(
    *,
    kind: Literal["csv_parse", "column_mapping", "observation_assembly"],
    implementation: str,
    implementation_version: str,
    input_ids: tuple[str, ...],
    parameters: tuple[TransformationParameter, ...],
    output_ids: tuple[str, ...],
) -> TransformationRecord:
    body = {
        "schema_version": TRANSFORMATION_SCHEMA_VERSION,
        "kind": kind,
        "implementation": implementation,
        "implementation_version": implementation_version,
        "input_ids": input_ids,
        "parameters": sorted(
            (parameter.model_dump(mode="python") for parameter in parameters),
            key=lambda item: item["name"],
        ),
        "output_ids": output_ids,
    }
    return TransformationRecord(
        transformation_id=content_id("transform", body),
        schema_version=TRANSFORMATION_SCHEMA_VERSION,
        kind=kind,
        implementation=implementation,
        implementation_version=implementation_version,
        input_ids=input_ids,
        parameters=tuple(sorted(parameters, key=lambda item: item.name)),
        output_ids=output_ids,
    )


class NormalisedSeries(_Model):
    series_id: str = Field(min_length=1)
    schema_version: Literal["1"]
    dtype: Literal["decimal"]
    shape: tuple[int]
    source_column: str = Field(min_length=1)
    role: ColumnRole
    source_unit: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    values: tuple[Decimal, ...] = Field(min_length=1)
    transformation_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _shape_matches_values(self) -> Self:
        if self.shape != (len(self.values),):
            raise ValueError("normalised series shape must match its values")
        return self


class CVLineage(_Model):
    environment_id: str = Field(min_length=1)
    source_artifact_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    import_profile_id: str = Field(min_length=1)
    transformation_ids: tuple[str, ...] = Field(min_length=1)


class NormalisedCVObservation(_Model):
    observation_id: str = Field(min_length=1)
    schema_version: Literal["1"]
    parser_version: str = Field(min_length=1)
    normalisation_version: str = Field(min_length=1)
    source_artifact_id: str = Field(min_length=1)
    import_profile_id: str = Field(min_length=1)
    data_origin: DataOrigin
    execution_mode: ExecutionMode
    environment_id: str = Field(min_length=1)
    row_count: int = Field(ge=1)
    series: tuple[NormalisedSeries, ...] = Field(min_length=2)
    metadata: CVMetadata
    transformation_ids: tuple[str, ...] = Field(min_length=1)
    provenance: CVLineage


class StructuralFinding(_Model):
    finding_id: str = Field(min_length=1)
    code: str = Field(min_length=1)
    status: Literal["pass", "warning", "blocking"]
    subject_id: str = Field(min_length=1)
    message: str = Field(min_length=1)


class TransformationGraph(_Model):
    source_artifact_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    records: tuple[TransformationRecord, ...] = Field(min_length=1)

    @property
    def is_closed(self) -> bool:
        available = {self.source_artifact_id}
        for record in self.records:
            if not set(record.input_ids).issubset(available):
                return False
            available.update(record.output_ids)
        return self.observation_id in available

    @model_validator(mode="after")
    def _lineage_is_closed(self) -> Self:
        record_ids = [record.transformation_id for record in self.records]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("transformation graph contains duplicate records")
        if not self.is_closed:
            raise ValueError("transformation graph does not close to the source artifact")
        return self


class NormalisationResult(_Model):
    observation: NormalisedCVObservation
    graph: TransformationGraph
    findings: tuple[StructuralFinding, ...]


__all__ = [
    "SERIES_DTYPE",
    "SERIES_SCHEMA_VERSION",
    "CVLineage",
    "NormalisationResult",
    "NormalisedCVObservation",
    "NormalisedSeries",
    "StructuralFinding",
    "TransformationGraph",
    "TransformationParameter",
    "TransformationRecord",
]

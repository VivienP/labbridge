"""Normalised galvanostatic-electrolysis observations and source lineage."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical import content_id
from .cv_observations import NormalisedSeries, StructuralFinding, TransformationGraph
from .electrolysis import (
    AuxiliaryAnalyticalResult,
    ElectrolysisColumnRole,
    ElectrolysisMetadata,
)
from .identity import DataOrigin, ExecutionMode

OBSERVATION_SCHEMA_VERSION = "1"
SERIES_SCHEMA_VERSION = "1"
SERIES_DTYPE = "decimal"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ElectrolysisLineage(_Model):
    environment_id: str = Field(min_length=1)
    source_artifact_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    import_profile_id: str = Field(min_length=1)
    auxiliary_source_artifact_ids: tuple[str, ...]
    transformation_ids: tuple[str, ...] = Field(min_length=1)


class NormalisedElectrolysisObservation(_Model):
    observation_id: str = Field(min_length=1)
    schema_version: Literal["1"]
    technique: Literal["galvanostatic_electrolysis"]
    parser_version: str = Field(min_length=1)
    normalisation_version: str = Field(min_length=1)
    source_artifact_id: str = Field(min_length=1)
    import_profile_id: str = Field(min_length=1)
    data_origin: DataOrigin
    execution_mode: ExecutionMode
    environment_id: str = Field(min_length=1)
    row_count: int = Field(ge=1)
    series: tuple[NormalisedSeries, ...] = Field(min_length=3)
    metadata: ElectrolysisMetadata
    auxiliary_results: tuple[AuxiliaryAnalyticalResult, ...]
    transformation_ids: tuple[str, ...] = Field(min_length=1)
    provenance: ElectrolysisLineage

    @model_validator(mode="after")
    def _electrical_axes_and_lineage_are_complete(self) -> Self:
        roles = [series.role for series in self.series]
        if roles.count("time") != 1 or roles.count("potential") != 1:
            raise ValueError("electrolysis observation requires one time and one potential series")
        if roles.count("current") + roles.count("current_density") != 1:
            raise ValueError("electrolysis observation requires one current series")
        if any(
            series.shape != (self.row_count,) or len(series.values) != self.row_count
            for series in self.series
        ):
            raise ValueError("every electrical series must match the declared row count")
        time_series = next(series for series in self.series if series.role == "time")
        if any(
            later <= earlier
            for earlier, later in zip(time_series.values, time_series.values[1:], strict=False)
        ):
            raise ValueError("electrolysis time values must be strictly increasing")
        if self.metadata.sampling_interval.state == "known":
            assert isinstance(self.metadata.sampling_interval.value, Decimal)
            time_factor = Decimal("0.001") if time_series.unit == "ms" else Decimal("1")
            interval_factor = (
                Decimal("0.001") if self.metadata.sampling_interval.unit == "ms" else Decimal("1")
            )
            expected_interval = self.metadata.sampling_interval.value * interval_factor
            intervals = tuple(
                (later - earlier) * time_factor
                for earlier, later in zip(time_series.values, time_series.values[1:], strict=False)
            )
            if any(interval != expected_interval for interval in intervals):
                raise ValueError("recorded time axis does not match the declared sampling interval")
        if any(
            result.electrical_source_artifact_id != self.source_artifact_id
            for result in self.auxiliary_results
        ):
            raise ValueError("auxiliary result names a different electrical source artifact")
        expected_auxiliary = tuple(
            sorted({result.source_artifact_id for result in self.auxiliary_results})
        )
        if self.provenance.auxiliary_source_artifact_ids != expected_auxiliary:
            raise ValueError("auxiliary result sources differ from electrolysis provenance")
        if self.transformation_ids != self.provenance.transformation_ids:
            raise ValueError("observation and provenance transformations differ")
        return self


def electrolysis_series_id(
    *,
    source_artifact_id: str,
    import_profile_id: str,
    schema_version: str,
    dtype: str,
    shape: Sequence[int],
    source_column: str,
    role: ElectrolysisColumnRole,
    source_unit: str,
    unit: str,
    values: Sequence[Decimal],
) -> str:
    return content_id(
        "electrolysis-series",
        {
            "source_artifact_id": source_artifact_id,
            "import_profile_id": import_profile_id,
            "schema_version": schema_version,
            "dtype": dtype,
            "shape": tuple(shape),
            "source_column": source_column,
            "role": role,
            "source_unit": source_unit,
            "unit": unit,
            "values": tuple(values),
        },
    )


def electrolysis_observation_id(observation: NormalisedElectrolysisObservation) -> str:
    return content_id(
        "electrolysis-observation",
        observation.model_dump(
            mode="python",
            exclude={"observation_id", "transformation_ids", "provenance"},
        ),
    )


class ElectrolysisNormalisationResult(_Model):
    observation: NormalisedElectrolysisObservation
    graph: TransformationGraph
    findings: tuple[StructuralFinding, ...]


__all__ = [
    "ElectrolysisLineage",
    "ElectrolysisNormalisationResult",
    "NormalisedElectrolysisObservation",
    "electrolysis_observation_id",
    "electrolysis_series_id",
]

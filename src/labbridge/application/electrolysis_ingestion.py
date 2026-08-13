"""Deterministic generic CSV ingestion for galvanostatic electrolysis."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, Protocol, cast

from labbridge.application.source_intake import RetrievedSource
from labbridge.domain.canonical import content_id
from labbridge.domain.cv_observations import (
    SERIES_DTYPE,
    SERIES_SCHEMA_VERSION,
    NormalisedSeries,
    StructuralFinding,
    TransformationGraph,
    TransformationParameter,
    TransformationRecord,
    _transformation_record,
)
from labbridge.domain.electrolysis import (
    ElectrolysisColumnRole,
    ElectrolysisImportProfile,
    electrolysis_import_profile_id,
)
from labbridge.domain.electrolysis_observations import (
    OBSERVATION_SCHEMA_VERSION,
    ElectrolysisLineage,
    ElectrolysisNormalisationResult,
    NormalisedElectrolysisObservation,
    electrolysis_series_id,
)
from labbridge.infrastructure.cv_csv import PARSER_VERSION
from labbridge.infrastructure.electrolysis_csv import parse_electrolysis_csv


def _finding(code: str, observation_id: str, message: str) -> StructuralFinding:
    return StructuralFinding(
        finding_id=content_id(
            "finding", {"code": code, "subject_id": observation_id, "status": "pass"}
        ),
        code=code,
        status="pass",
        subject_id=observation_id,
        message=message,
    )


def normalise_electrolysis(
    source: RetrievedSource,
    profile: ElectrolysisImportProfile,
    *,
    producing_version: str,
    auxiliary_sources: Mapping[str, RetrievedSource] | None = None,
) -> ElectrolysisNormalisationResult:
    """Normalise the electrical time series without deriving chemical or product claims."""
    retained_auxiliary = dict(auxiliary_sources or {})
    expected_auxiliary = {result.source_artifact_id for result in profile.auxiliary_results}
    if set(retained_auxiliary) != expected_auxiliary:
        raise ValueError("every auxiliary result requires its retained auxiliary source artifact")
    for source_id, retained in retained_auxiliary.items():
        artifact = retained.artifact
        if artifact.source_artifact_id != source_id or artifact.state != "committed":
            raise ValueError("auxiliary source artifact identity or state is invalid")
        if (
            artifact.byte_size != len(retained.data)
            or artifact.sha256 != hashlib.sha256(retained.data).hexdigest()
        ):
            raise ValueError("auxiliary source artifact bytes do not match retained metadata")
        if (
            artifact.data_origin != source.artifact.data_origin
            or artifact.execution_mode != source.artifact.execution_mode
        ):
            raise ValueError("auxiliary and electrical sources require the same origin and mode")
    if any(
        result.electrical_source_artifact_id != source.artifact.source_artifact_id
        for result in profile.auxiliary_results
    ):
        raise ValueError("auxiliary result names a different electrical source artifact")
    parsed = parse_electrolysis_csv(source.data, profile)
    if parsed.row_count < 1:
        raise ValueError("a normalised electrolysis observation requires at least one data row")
    artifact = source.artifact
    profile_id = electrolysis_import_profile_id(profile)
    parsed_table_id = content_id(
        "electrolysis-csv-table",
        {
            "source_artifact_id": artifact.source_artifact_id,
            "import_profile_id": profile_id,
            "parser_version": parsed.parser_version,
            "headers": parsed.headers,
            "row_count": parsed.row_count,
        },
    )
    records: list[TransformationRecord] = [
        _transformation_record(
            kind="csv_parse",
            implementation="labbridge.infrastructure.electrolysis_csv.parse_electrolysis_csv",
            implementation_version=PARSER_VERSION,
            input_ids=(artifact.source_artifact_id,),
            parameters=(
                TransformationParameter(name="encoding", value=profile.encoding),
                TransformationParameter(name="delimiter", value=profile.delimiter),
                TransformationParameter(
                    name="decimal_convention", value=profile.decimal_convention
                ),
                TransformationParameter(name="header_row", value=str(profile.header_row)),
                TransformationParameter(
                    name="missing_value_tokens",
                    value="|".join(sorted(profile.missing_value_tokens)),
                ),
            ),
            output_ids=(parsed_table_id,),
        )
    ]
    series_models: list[NormalisedSeries] = []
    series_ids: list[str] = []
    for parsed_series in parsed.series:
        series_id = electrolysis_series_id(
            source_artifact_id=artifact.source_artifact_id,
            import_profile_id=profile_id,
            schema_version=SERIES_SCHEMA_VERSION,
            dtype=SERIES_DTYPE,
            shape=(len(parsed_series.values),),
            source_column=parsed_series.source_column,
            role=cast(ElectrolysisColumnRole, parsed_series.role),
            source_unit=parsed_series.source_unit,
            unit=parsed_series.target_unit,
            values=parsed_series.values,
        )
        record = _transformation_record(
            kind="column_mapping",
            implementation="labbridge.infrastructure.cv_csv.unit_conversion",
            implementation_version=parsed.parser_version,
            input_ids=(parsed_table_id,),
            parameters=(
                TransformationParameter(name="source_column", value=parsed_series.source_column),
                TransformationParameter(name="role", value=parsed_series.role),
                TransformationParameter(name="source_unit", value=parsed_series.source_unit),
                TransformationParameter(name="target_unit", value=parsed_series.target_unit),
            ),
            output_ids=(series_id,),
        )
        records.append(record)
        series_ids.append(series_id)
        series_models.append(
            NormalisedSeries(
                series_id=series_id,
                schema_version=SERIES_SCHEMA_VERSION,
                dtype=SERIES_DTYPE,
                shape=(len(parsed_series.values),),
                source_column=parsed_series.source_column,
                role=parsed_series.role,
                source_unit=parsed_series.source_unit,
                unit=parsed_series.target_unit,
                values=parsed_series.values,
                transformation_id=record.transformation_id,
            )
        )
    observation_body = {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "technique": profile.technique,
        "parser_version": parsed.parser_version,
        "normalisation_version": producing_version,
        "source_artifact_id": artifact.source_artifact_id,
        "import_profile_id": profile_id,
        "data_origin": artifact.data_origin,
        "execution_mode": artifact.execution_mode,
        "environment_id": profile.environment_id,
        "row_count": parsed.row_count,
        "series": [series.model_dump(mode="python") for series in series_models],
        "metadata": profile.metadata,
        "auxiliary_results": profile.auxiliary_results,
    }
    observation_id = content_id("electrolysis-observation", observation_body)
    records.append(
        _transformation_record(
            kind="observation_assembly",
            implementation=("labbridge.application.electrolysis_ingestion.normalise_electrolysis"),
            implementation_version=producing_version,
            input_ids=tuple(series_ids),
            parameters=(
                TransformationParameter(
                    name="observation_schema_version", value=OBSERVATION_SCHEMA_VERSION
                ),
            ),
            output_ids=(observation_id,),
        )
    )
    transformation_ids = tuple(record.transformation_id for record in records)
    auxiliary_source_ids = tuple(
        sorted({result.source_artifact_id for result in profile.auxiliary_results})
    )
    lineage = ElectrolysisLineage(
        environment_id=profile.environment_id,
        source_artifact_id=artifact.source_artifact_id,
        source_sha256=artifact.sha256,
        import_profile_id=profile_id,
        auxiliary_source_artifact_ids=auxiliary_source_ids,
        transformation_ids=transformation_ids,
    )
    observation = NormalisedElectrolysisObservation(
        observation_id=observation_id,
        transformation_ids=transformation_ids,
        provenance=lineage,
        **observation_body,
    )
    graph = TransformationGraph(
        source_artifact_id=artifact.source_artifact_id,
        observation_id=observation_id,
        records=tuple(records),
    )
    findings = (
        _finding("csv.structure.valid", observation_id, "CSV rows and headers are valid."),
        _finding(
            "electrolysis.electrical_axes.valid",
            observation_id,
            "Required time, current, and potential axes are explicitly mapped.",
        ),
        _finding(
            "unit.mapping.valid",
            observation_id,
            "Every electrical column has a supported explicit unit mapping.",
        ),
        _finding(
            "lineage.closed",
            observation_id,
            "Every normalised electrical series closes to the retained source artifact.",
        ),
    )
    return ElectrolysisNormalisationResult(
        observation=observation,
        graph=graph,
        findings=findings,
    )


class ElectrolysisIngestionError(Exception):
    code: ClassVar[str] = "electrolysis_ingestion_error"


class ElectrolysisProfileNotFoundError(ElectrolysisIngestionError):
    code = "electrolysis_profile_not_found"


class ElectrolysisObservationNotFoundError(ElectrolysisIngestionError):
    code = "electrolysis_observation_not_found"


class ElectrolysisObservationIntegrityError(ElectrolysisIngestionError):
    code = "electrolysis_observation_integrity_mismatch"


class ElectrolysisIdempotencyConflictError(ElectrolysisIngestionError):
    code = "electrolysis_idempotency_key_reused"


class SourceReader(Protocol):
    def retrieve(self, source_artifact_id: str) -> RetrievedSource: ...


class ElectrolysisRecordRepository(Protocol):
    def put_profile(
        self, item: ElectrolysisImportProfile, *, idempotency_key: str | None = None
    ) -> tuple[str, bool]: ...

    def get_profile(self, profile_id: str) -> ElectrolysisImportProfile | None: ...

    def put_normalisation(
        self,
        result: ElectrolysisNormalisationResult,
        *,
        idempotency_key: str | None = None,
    ) -> bool: ...

    def get_normalisation(self, observation_id: str) -> ElectrolysisNormalisationResult | None: ...


@dataclass(frozen=True)
class StoredElectrolysisProfile:
    profile_id: str
    profile: ElectrolysisImportProfile
    replayed: bool


@dataclass(frozen=True)
class StoredElectrolysisNormalisation:
    result: ElectrolysisNormalisationResult
    replayed: bool


class ElectrolysisIngestionService:
    """Application boundary for electrolysis profiles and retained normalisations."""

    def __init__(
        self,
        sources: SourceReader,
        records: ElectrolysisRecordRepository,
        *,
        producing_version: str,
    ) -> None:
        self._sources = sources
        self._records = records
        self._producing_version = producing_version

    def create_profile(
        self,
        profile: ElectrolysisImportProfile,
        *,
        idempotency_key: str | None = None,
    ) -> StoredElectrolysisProfile:
        profile_id, replayed = self._records.put_profile(profile, idempotency_key=idempotency_key)
        return StoredElectrolysisProfile(profile_id, profile, replayed)

    def get_profile(self, profile_id: str) -> StoredElectrolysisProfile:
        profile = self._records.get_profile(profile_id)
        if profile is None:
            raise ElectrolysisProfileNotFoundError(profile_id)
        return StoredElectrolysisProfile(profile_id, profile, True)

    def normalise(
        self,
        source_artifact_id: str,
        profile_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> StoredElectrolysisNormalisation:
        profile = self._records.get_profile(profile_id)
        if profile is None:
            raise ElectrolysisProfileNotFoundError(profile_id)
        source = self._sources.retrieve(source_artifact_id)
        auxiliary_ids = {result.source_artifact_id for result in profile.auxiliary_results}
        auxiliary_sources = {item_id: self._sources.retrieve(item_id) for item_id in auxiliary_ids}
        result = normalise_electrolysis(
            source,
            profile,
            producing_version=self._producing_version,
            auxiliary_sources=auxiliary_sources,
        )
        replayed = self._records.put_normalisation(result, idempotency_key=idempotency_key)
        return StoredElectrolysisNormalisation(result, replayed)

    def get_normalisation(self, observation_id: str) -> StoredElectrolysisNormalisation:
        result = self._records.get_normalisation(observation_id)
        if result is None:
            raise ElectrolysisObservationNotFoundError(observation_id)
        return StoredElectrolysisNormalisation(result, True)


__all__ = [
    "ElectrolysisIdempotencyConflictError",
    "ElectrolysisIngestionError",
    "ElectrolysisIngestionService",
    "ElectrolysisObservationIntegrityError",
    "ElectrolysisObservationNotFoundError",
    "ElectrolysisProfileNotFoundError",
    "ElectrolysisRecordRepository",
    "StoredElectrolysisNormalisation",
    "StoredElectrolysisProfile",
    "normalise_electrolysis",
]

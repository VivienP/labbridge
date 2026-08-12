"""Framework-independent use cases for explicit generic CV CSV ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Protocol

from labbridge.application.source_intake import RetrievedSource
from labbridge.domain.canonical import content_id
from labbridge.domain.cv import CSVFormat, CVImportProfile, import_profile_id
from labbridge.domain.cv_observations import (
    OBSERVATION_SCHEMA_VERSION,
    SERIES_DTYPE,
    SERIES_SCHEMA_VERSION,
    CVLineage,
    NormalisationResult,
    NormalisedCVObservation,
    NormalisedSeries,
    StructuralFinding,
    TransformationGraph,
    TransformationParameter,
    TransformationRecord,
    _transformation_record,
)
from labbridge.infrastructure.cv_csv import PARSER_VERSION, ParsedSeries, inspect_csv, parse_cv_csv


def _series_identity(source_artifact_id: str, profile_id: str, series: ParsedSeries) -> str:
    shape = (len(series.values),)
    return content_id(
        "cv-series",
        {
            "source_artifact_id": source_artifact_id,
            "import_profile_id": profile_id,
            "schema_version": SERIES_SCHEMA_VERSION,
            "dtype": SERIES_DTYPE,
            "shape": shape,
            "source_column": series.source_column,
            "role": series.role,
            "source_unit": series.source_unit,
            "unit": series.target_unit,
            "values": series.values,
        },
    )


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


def normalise_cv(
    source: RetrievedSource, profile: CVImportProfile, *, producing_version: str
) -> NormalisationResult:
    """Build the deterministic observation and every declared transformation from exact bytes."""
    parsed = parse_cv_csv(source.data, profile)
    if parsed.row_count < 1:
        raise ValueError("a normalised CV observation requires at least one data row")
    artifact = source.artifact
    profile_identity = import_profile_id(profile)
    parsed_table_id = content_id(
        "csv-table",
        {
            "source_artifact_id": artifact.source_artifact_id,
            "import_profile_id": profile_identity,
            "parser_version": parsed.parser_version,
            "headers": parsed.headers,
            "row_count": parsed.row_count,
        },
    )
    records: list[TransformationRecord] = [
        _transformation_record(
            kind="csv_parse",
            implementation="labbridge.infrastructure.cv_csv.parse_cv_csv",
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
        series_id = _series_identity(artifact.source_artifact_id, profile_identity, parsed_series)
        record = _transformation_record(
            kind="column_mapping",
            implementation="labbridge.infrastructure.cv_csv.unit_conversion",
            implementation_version=PARSER_VERSION,
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
    observation_id = content_id(
        "cv-observation",
        {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "parser_version": parsed.parser_version,
            "normalisation_version": producing_version,
            "source_artifact_id": artifact.source_artifact_id,
            "import_profile_id": profile_identity,
            "data_origin": artifact.data_origin,
            "execution_mode": artifact.execution_mode,
            "environment_id": profile.environment_id,
            "row_count": parsed.row_count,
            "series": [item.model_dump(mode="python") for item in series_models],
            "metadata": profile.metadata,
        },
    )
    records.append(
        _transformation_record(
            kind="observation_assembly",
            implementation="labbridge.application.cv_ingestion.normalise_cv",
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
    lineage = CVLineage(
        environment_id=profile.environment_id,
        source_artifact_id=artifact.source_artifact_id,
        source_sha256=artifact.sha256,
        import_profile_id=profile_identity,
        transformation_ids=transformation_ids,
    )
    observation = NormalisedCVObservation(
        observation_id=observation_id,
        schema_version=OBSERVATION_SCHEMA_VERSION,
        parser_version=parsed.parser_version,
        normalisation_version=producing_version,
        source_artifact_id=artifact.source_artifact_id,
        import_profile_id=profile_identity,
        data_origin=artifact.data_origin,
        execution_mode=artifact.execution_mode,
        environment_id=profile.environment_id,
        row_count=parsed.row_count,
        series=tuple(series_models),
        metadata=profile.metadata,
        transformation_ids=transformation_ids,
        provenance=lineage,
    )
    graph = TransformationGraph(
        source_artifact_id=artifact.source_artifact_id,
        observation_id=observation_id,
        records=tuple(records),
    )
    findings = (
        _finding(
            "csv.structure.valid", observation_id, "CSV rows and headers are structurally valid."
        ),
        _finding(
            "cv.axes.valid", observation_id, "Required potential and current axes are mapped."
        ),
        _finding(
            "unit.mapping.valid",
            observation_id,
            "Every scientific column has a supported explicit unit mapping.",
        ),
        _finding(
            "lineage.closed",
            observation_id,
            "Every normalised series closes to the retained source artifact.",
        ),
    )
    return NormalisationResult(observation=observation, graph=graph, findings=findings)


class CVIngestionError(Exception):
    code: ClassVar[str] = "cv_ingestion_error"


class ImportProfileNotFoundError(CVIngestionError):
    code = "import_profile_not_found"

    def __init__(self, profile_id: str) -> None:
        super().__init__(f"import profile `{profile_id}` does not exist")


class NormalisedObservationNotFoundError(CVIngestionError):
    code = "normalised_observation_not_found"

    def __init__(self, observation_id: str) -> None:
        super().__init__(f"normalised observation `{observation_id}` does not exist")


class NormalisedObservationIntegrityError(CVIngestionError):
    code = "normalised_observation_integrity_mismatch"

    def __init__(self, observation_id: str) -> None:
        super().__init__(
            f"normalised observation `{observation_id}` no longer matches its retained object"
        )


class CVIdempotencyConflictError(CVIngestionError):
    code = "cv_idempotency_key_reused"

    def __init__(self, idempotency_key: str) -> None:
        super().__init__(f"idempotency key `{idempotency_key}` was reused for a different request")


class SourceReader(Protocol):
    def retrieve(self, source_artifact_id: str) -> RetrievedSource: ...


class CVRecordRepository(Protocol):
    def put_profile(
        self, item: CVImportProfile, *, idempotency_key: str | None = None
    ) -> tuple[str, bool]: ...

    def get_profile(self, profile_id: str) -> CVImportProfile | None: ...

    def put_normalisation(
        self, result: NormalisationResult, *, idempotency_key: str | None = None
    ) -> bool: ...

    def get_normalisation(self, observation_id: str) -> NormalisationResult | None: ...


@dataclass(frozen=True)
class StoredProfile:
    profile_id: str
    profile: CVImportProfile
    replayed: bool


@dataclass(frozen=True)
class StoredNormalisation:
    result: NormalisationResult
    replayed: bool


@dataclass(frozen=True)
class PlotSeries:
    observation_id: str
    data_origin: str
    execution_mode: str
    environment_id: str
    series: tuple[NormalisedSeries, ...]
    provenance: CVLineage


@dataclass(frozen=True)
class SourceInspection:
    source_artifact_id: str
    source_sha256: str
    headers: tuple[str, ...]
    row_count: int


class CVIngestionService:
    """The shared profile, normalisation, and plot-series application boundary."""

    def __init__(
        self,
        sources: SourceReader,
        records: CVRecordRepository,
        *,
        producing_version: str,
    ) -> None:
        self._sources = sources
        self._records = records
        self._producing_version = producing_version

    def create_profile(
        self, profile: CVImportProfile, *, idempotency_key: str | None = None
    ) -> StoredProfile:
        profile_id, replayed = self._records.put_profile(profile, idempotency_key=idempotency_key)
        return StoredProfile(profile_id=profile_id, profile=profile, replayed=replayed)

    def inspect(self, source_artifact_id: str, csv_format: CSVFormat) -> SourceInspection:
        source = self._sources.retrieve(source_artifact_id)
        inspected = inspect_csv(source.data, csv_format)
        return SourceInspection(
            source_artifact_id=source.artifact.source_artifact_id,
            source_sha256=source.artifact.sha256,
            headers=inspected.headers,
            row_count=inspected.row_count,
        )

    def get_profile(self, profile_id: str) -> StoredProfile:
        profile = self._records.get_profile(profile_id)
        if profile is None:
            raise ImportProfileNotFoundError(profile_id)
        return StoredProfile(profile_id=profile_id, profile=profile, replayed=True)

    def normalise(
        self,
        source_artifact_id: str,
        profile_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> StoredNormalisation:
        profile = self._records.get_profile(profile_id)
        if profile is None:
            raise ImportProfileNotFoundError(profile_id)
        source = self._sources.retrieve(source_artifact_id)
        result = normalise_cv(source, profile, producing_version=self._producing_version)
        replayed = self._records.put_normalisation(result, idempotency_key=idempotency_key)
        return StoredNormalisation(result=result, replayed=replayed)

    def get_normalisation(self, observation_id: str) -> StoredNormalisation:
        result = self._records.get_normalisation(observation_id)
        if result is None:
            raise NormalisedObservationNotFoundError(observation_id)
        return StoredNormalisation(result=result, replayed=True)

    def plot_series(self, observation_id: str) -> PlotSeries:
        result = self.get_normalisation(observation_id).result
        observation = result.observation
        return PlotSeries(
            observation_id=observation.observation_id,
            data_origin=observation.data_origin,
            execution_mode=observation.execution_mode,
            environment_id=observation.environment_id,
            series=observation.series,
            provenance=observation.provenance,
        )


__all__ = [
    "CVIdempotencyConflictError",
    "CVIngestionError",
    "CVIngestionService",
    "CVRecordRepository",
    "ImportProfileNotFoundError",
    "NormalisedObservationIntegrityError",
    "NormalisedObservationNotFoundError",
    "PlotSeries",
    "SourceInspection",
    "SourceReader",
    "StoredNormalisation",
    "StoredProfile",
]

"""FastAPI adapter for explicit generic CV CSV ingestion."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict

from labbridge.application.cv_ingestion import (
    CVIngestionError,
    CVIngestionService,
    ImportProfileNotFoundError,
    NormalisedObservationNotFoundError,
)
from labbridge.application.source_intake import SourceIntakeError
from labbridge.domain.cv import CSVFormat, CVImportProfile
from labbridge.domain.cv_observations import CVLineage, NormalisationResult, NormalisedSeries
from labbridge.domain.idempotency import IdempotencyKeyError, normalise_idempotency_key
from labbridge.infrastructure.cv_csv import CsvParseError


class InspectionRequest(CSVFormat):
    source_artifact_id: str


class NormalisationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_artifact_id: str
    profile_id: str


class SourceInspectionView(BaseModel):
    source_artifact_id: str
    source_sha256: str
    headers: tuple[str, ...]
    row_count: int


class ProfileView(BaseModel):
    profile_id: str
    profile: CVImportProfile
    replayed: bool


class NormalisationView(BaseModel):
    result: NormalisationResult
    replayed: bool


class PlotSeriesView(BaseModel):
    observation_id: str
    data_origin: str
    execution_mode: str
    environment_id: str
    series: tuple[NormalisedSeries, ...]
    provenance: CVLineage


def _idempotency(key: str | None) -> str:
    try:
        return normalise_idempotency_key(key)
    except IdempotencyKeyError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": error.code,
                "message": str(error),
            },
        ) from error


def _error(error: Exception) -> HTTPException:
    if isinstance(error, ImportProfileNotFoundError | NormalisedObservationNotFoundError):
        http_status = status.HTTP_404_NOT_FOUND
    elif isinstance(error, CsvParseError | ValueError):
        http_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    else:
        http_status = status.HTTP_409_CONFLICT
    code = getattr(error, "code", "cv_ingestion_error")
    return HTTPException(status_code=http_status, detail={"code": code, "message": str(error)})


def register_cv_routes(app: FastAPI, service: Callable[[], CVIngestionService]) -> None:
    @app.post("/cv/source-inspections")
    def inspect_source(request: InspectionRequest) -> SourceInspectionView:
        try:
            result = service().inspect(
                request.source_artifact_id,
                CSVFormat(
                    encoding=request.encoding,
                    delimiter=request.delimiter,
                    header_row=request.header_row,
                ),
            )
        except (CVIngestionError, SourceIntakeError, CsvParseError, ValueError) as error:
            raise _error(error) from error
        return SourceInspectionView(
            source_artifact_id=result.source_artifact_id,
            source_sha256=result.source_sha256,
            headers=result.headers,
            row_count=result.row_count,
        )

    @app.post("/cv/import-profiles", status_code=status.HTTP_201_CREATED)
    def create_profile(
        profile: CVImportProfile,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ProfileView:
        key = _idempotency(idempotency_key)
        try:
            stored = service().create_profile(profile, idempotency_key=key)
        except CVIngestionError as error:
            raise _error(error) from error
        if stored.replayed:
            response.status_code = status.HTTP_200_OK
        return ProfileView(
            profile_id=stored.profile_id, profile=stored.profile, replayed=stored.replayed
        )

    @app.get("/cv/import-profiles/{profile_id}")
    def read_profile(profile_id: str) -> ProfileView:
        try:
            stored = service().get_profile(profile_id)
        except CVIngestionError as error:
            raise _error(error) from error
        return ProfileView(profile_id=stored.profile_id, profile=stored.profile, replayed=True)

    @app.post("/cv/normalisations", status_code=status.HTTP_201_CREATED)
    def normalise(
        request: NormalisationRequest,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> NormalisationView:
        key = _idempotency(idempotency_key)
        try:
            stored = service().normalise(
                request.source_artifact_id,
                request.profile_id,
                idempotency_key=key,
            )
        except (CVIngestionError, SourceIntakeError, CsvParseError, ValueError) as error:
            raise _error(error) from error
        if stored.replayed:
            response.status_code = status.HTTP_200_OK
        return NormalisationView(result=stored.result, replayed=stored.replayed)

    @app.get("/cv/normalised-observations/{observation_id}")
    def read_normalisation(observation_id: str) -> NormalisationView:
        try:
            stored = service().get_normalisation(observation_id)
        except CVIngestionError as error:
            raise _error(error) from error
        return NormalisationView(result=stored.result, replayed=True)

    @app.get("/cv/normalised-observations/{observation_id}/plot-series")
    def plot_series(observation_id: str) -> PlotSeriesView:
        try:
            plot = service().plot_series(observation_id)
        except CVIngestionError as error:
            raise _error(error) from error
        return PlotSeriesView(
            observation_id=plot.observation_id,
            data_origin=plot.data_origin,
            execution_mode=plot.execution_mode,
            environment_id=plot.environment_id,
            series=plot.series,
            provenance=plot.provenance,
        )


__all__ = ["register_cv_routes"]

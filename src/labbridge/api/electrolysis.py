"""FastAPI adapter for explicit galvanostatic-electrolysis ingestion."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict

from labbridge.application.electrolysis_ingestion import (
    ElectrolysisIngestionError,
    ElectrolysisIngestionService,
    ElectrolysisObservationNotFoundError,
    ElectrolysisProfileNotFoundError,
    StoredElectrolysisNormalisation,
    StoredElectrolysisProfile,
)
from labbridge.application.source_intake import SourceIntakeError
from labbridge.domain.electrolysis import ElectrolysisImportProfile
from labbridge.domain.electrolysis_observations import ElectrolysisNormalisationResult
from labbridge.domain.idempotency import IdempotencyKeyError, normalise_idempotency_key
from labbridge.infrastructure.cv_csv import CsvParseError


class ElectrolysisNormalisationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_artifact_id: str
    profile_id: str


class ElectrolysisProfileView(BaseModel):
    profile_id: str
    profile: ElectrolysisImportProfile
    replayed: bool


class ElectrolysisNormalisationView(BaseModel):
    result: ElectrolysisNormalisationResult
    replayed: bool


def _idempotency(key: str | None) -> str:
    try:
        return normalise_idempotency_key(key)
    except IdempotencyKeyError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": error.code, "message": str(error)},
        ) from error


def _error(error: Exception) -> HTTPException:
    if isinstance(
        error,
        ElectrolysisProfileNotFoundError | ElectrolysisObservationNotFoundError,
    ):
        http_status = status.HTTP_404_NOT_FOUND
    elif isinstance(error, CsvParseError | ValueError):
        http_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    else:
        http_status = status.HTTP_409_CONFLICT
    return HTTPException(
        status_code=http_status,
        detail={
            "code": getattr(error, "code", "electrolysis_ingestion_error"),
            "message": str(error),
        },
    )


def _profile_view(stored: StoredElectrolysisProfile, response: Response) -> ElectrolysisProfileView:
    if stored.replayed:
        response.status_code = status.HTTP_200_OK
    return ElectrolysisProfileView(
        profile_id=stored.profile_id, profile=stored.profile, replayed=stored.replayed
    )


def _normalisation_view(
    stored: StoredElectrolysisNormalisation, response: Response
) -> ElectrolysisNormalisationView:
    if stored.replayed:
        response.status_code = status.HTTP_200_OK
    return ElectrolysisNormalisationView(result=stored.result, replayed=stored.replayed)


def register_electrolysis_routes(
    app: FastAPI, service: Callable[[], ElectrolysisIngestionService]
) -> None:
    @app.post("/electrolysis/import-profiles", status_code=status.HTTP_201_CREATED)
    def create_profile(
        profile: ElectrolysisImportProfile,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ElectrolysisProfileView:
        key = _idempotency(idempotency_key)
        try:
            stored = service().create_profile(profile, idempotency_key=key)
        except ElectrolysisIngestionError as error:
            raise _error(error) from error
        return _profile_view(stored, response)

    @app.get("/electrolysis/import-profiles/{profile_id}")
    def read_profile(profile_id: str) -> ElectrolysisProfileView:
        try:
            stored = service().get_profile(profile_id)
        except ElectrolysisIngestionError as error:
            raise _error(error) from error
        return ElectrolysisProfileView(
            profile_id=stored.profile_id, profile=stored.profile, replayed=True
        )

    @app.post("/electrolysis/normalisations", status_code=status.HTTP_201_CREATED)
    def normalise(
        request: ElectrolysisNormalisationRequest,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ElectrolysisNormalisationView:
        key = _idempotency(idempotency_key)
        try:
            stored = service().normalise(
                request.source_artifact_id,
                request.profile_id,
                idempotency_key=key,
            )
        except (
            ElectrolysisIngestionError,
            SourceIntakeError,
            CsvParseError,
            ValueError,
        ) as error:
            raise _error(error) from error
        return _normalisation_view(stored, response)

    @app.get("/electrolysis/normalised-observations/{observation_id}")
    def read_normalisation(observation_id: str) -> ElectrolysisNormalisationView:
        try:
            stored = service().get_normalisation(observation_id)
        except ElectrolysisIngestionError as error:
            raise _error(error) from error
        return ElectrolysisNormalisationView(result=stored.result, replayed=True)


__all__ = ["register_electrolysis_routes"]

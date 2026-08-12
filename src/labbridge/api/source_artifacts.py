"""FastAPI adapter for the opaque source-artifact application service."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel

from labbridge.application.source_intake import (
    IntakeConflictError,
    IntakeSource,
    SourceArtifactService,
    SourceIntakeError,
    SourceIntegrityError,
    SourceNotFoundError,
    SourceNotReadyError,
)
from labbridge.domain.identity import DataOrigin, ExecutionMode
from labbridge.domain.source_artifacts import SourceArtifact


class SourceArtifactView(BaseModel):
    source_artifact_id: str
    filename: str
    media_type: str
    byte_size: int
    sha256: str
    data_origin: str
    execution_mode: str
    state: str
    object_uri: str
    replayed: bool = False


def _error(error: SourceIntakeError) -> HTTPException:
    if isinstance(error, SourceNotFoundError):
        http_status = status.HTTP_404_NOT_FOUND
    elif isinstance(error, IntakeConflictError | SourceNotReadyError | SourceIntegrityError):
        http_status = status.HTTP_409_CONFLICT
    else:
        http_status = status.HTTP_400_BAD_REQUEST
    return HTTPException(
        status_code=http_status,
        detail={"code": error.code, "message": str(error)},
    )


def _view(artifact: SourceArtifact, *, replayed: bool = False) -> SourceArtifactView:
    return SourceArtifactView(**artifact.model_dump(mode="json"), replayed=replayed)


def register_source_routes(app: FastAPI, service: Callable[[], SourceArtifactService]) -> None:
    @app.post("/source-artifacts", status_code=status.HTTP_201_CREATED)
    async def intake_source_artifact(
        request: Request,
        response: Response,
        filename: str,
        data_origin: DataOrigin,
        execution_mode: ExecutionMode,
    ) -> SourceArtifactView:
        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "idempotency_key_required",
                    "message": "source intake requires an Idempotency-Key header",
                },
            )
        content_type = request.headers.get("Content-Type")
        if not content_type:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "media_type_required",
                    "message": "source intake requires a Content-Type header",
                },
            )
        try:
            result = service().intake(
                IntakeSource(
                    intake_id=idempotency_key,
                    data=await request.body(),
                    filename=filename,
                    media_type=content_type,
                    data_origin=data_origin,
                    execution_mode=execution_mode,
                )
            )
        except SourceIntakeError as error:
            raise _error(error) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return _view(result.artifact, replayed=result.replayed)

    @app.get("/source-artifacts/{source_artifact_id}")
    def read_source_artifact(source_artifact_id: str) -> SourceArtifactView:
        try:
            artifact = service().lookup(source_artifact_id)
        except SourceIntakeError as error:
            raise _error(error) from error
        return _view(artifact)

    @app.get("/source-artifacts/{source_artifact_id}/content")
    def read_source_content(source_artifact_id: str) -> Response:
        try:
            retrieved = service().retrieve(source_artifact_id)
        except SourceIntakeError as error:
            raise _error(error) from error
        return Response(
            content=retrieved.data,
            media_type=retrieved.artifact.media_type,
            headers={"ETag": f'"sha256:{retrieved.artifact.sha256}"'},
        )


__all__ = ["register_source_routes"]

"""FastAPI adapter for Experiment Passports and verified Packages."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, Header, HTTPException, Response, status
from fastapi.responses import Response as FastAPIResponse
from pydantic import BaseModel, ConfigDict, Field

from labbridge.application.experiments import (
    ExperimentApplicationError,
    ExperimentIdempotencyConflictError,
    ExperimentNotFoundError,
    ExperimentService,
    PackageNotFoundError,
    PassportNotFoundError,
    StoredExperiment,
    StoredPackage,
    StoredPassport,
    UserAssertionCommand,
)
from labbridge.domain.experiments import (
    AssertionTransformation,
    AssertionValue,
    Experiment,
    ExperimentVersionConflictError,
    RequirementClass,
    ValidationRun,
)
from labbridge.domain.idempotency import IdempotencyKeyError, normalise_idempotency_key
from labbridge.evidence.experiment_package import ExperimentPackage
from labbridge.evidence.passport import ExperimentPassport


class CreateExperimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_id: str = Field(min_length=1)
    expected_experiment_version: int = Field(ge=0)


class UserAssertionRequest(BaseModel):
    """HTTP user edits intentionally expose no origin selector."""

    model_config = ConfigDict(extra="forbid")

    expected_experiment_version: int = Field(ge=1)
    field_name: str = Field(min_length=1)
    requirement_class: RequirementClass
    transformation: AssertionTransformation
    value: AssertionValue
    evidence_note: str = Field(min_length=1)
    supplements_assertion_id: str | None = None
    supersedes_assertion_id: str | None = None


class ExpectedVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_experiment_version: int = Field(ge=1)


class CreatePackageRequest(ExpectedVersionRequest):
    passport_id: str = Field(min_length=1)


class ExperimentView(BaseModel):
    experiment: Experiment
    replayed: bool


class ValidationView(BaseModel):
    validation: ValidationRun
    replayed: bool


class PassportView(BaseModel):
    passport: ExperimentPassport
    replayed: bool


class PackageView(BaseModel):
    package: ExperimentPackage
    replayed: bool


def _key(value: str | None) -> str:
    try:
        return normalise_idempotency_key(value)
    except IdempotencyKeyError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": error.code, "message": str(error)},
        ) from error


def _error(error: Exception) -> HTTPException:
    if isinstance(
        error,
        ExperimentNotFoundError | PassportNotFoundError | PackageNotFoundError,
    ):
        http_status = status.HTTP_404_NOT_FOUND
    elif isinstance(error, ExperimentIdempotencyConflictError | ExperimentVersionConflictError) or (
        "blocking validation findings" in str(error)
    ):
        http_status = status.HTTP_409_CONFLICT
    else:
        http_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    return HTTPException(
        status_code=http_status,
        detail={
            "code": getattr(error, "code", "experiment_request_invalid"),
            "message": str(error),
        },
    )


def _experiment_view(stored: StoredExperiment, response: Response) -> ExperimentView:
    if stored.replayed:
        response.status_code = status.HTTP_200_OK
    return ExperimentView(experiment=stored.experiment, replayed=stored.replayed)


def _passport_view(stored: StoredPassport, response: Response) -> PassportView:
    if stored.replayed:
        response.status_code = status.HTTP_200_OK
    return PassportView(passport=stored.passport, replayed=stored.replayed)


def _package_view(stored: StoredPackage, response: Response) -> PackageView:
    if stored.replayed:
        response.status_code = status.HTTP_200_OK
    return PackageView(package=stored.package, replayed=stored.replayed)


def register_experiment_routes(  # noqa: PLR0915 - routes translate one bounded service contract
    app: FastAPI, service: Callable[[], ExperimentService]
) -> None:
    @app.post("/experiments", status_code=status.HTTP_201_CREATED)
    def create_experiment_route(
        request: CreateExperimentRequest,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ExperimentView:
        try:
            stored = service().create_experiment(
                request.observation_id,
                expected_version=request.expected_experiment_version,
                idempotency_key=_key(idempotency_key),
            )
        except (ExperimentApplicationError, ValueError) as error:
            raise _error(error) from error
        return _experiment_view(stored, response)

    @app.get("/experiments/{experiment_id}")
    def get_experiment_route(experiment_id: str) -> ExperimentView:
        try:
            stored = service().get_experiment(experiment_id)
        except ExperimentApplicationError as error:
            raise _error(error) from error
        return ExperimentView(experiment=stored.experiment, replayed=True)

    @app.post("/experiments/{experiment_id}/assertions", status_code=status.HTTP_201_CREATED)
    def add_assertion_route(
        experiment_id: str,
        request: UserAssertionRequest,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ExperimentView:
        try:
            stored = service().add_user_assertion(
                experiment_id,
                expected_version=request.expected_experiment_version,
                idempotency_key=_key(idempotency_key),
                command=UserAssertionCommand(
                    field_name=request.field_name,
                    requirement_class=request.requirement_class,
                    transformation=request.transformation,
                    value=request.value,
                    evidence_note=request.evidence_note,
                    supplements_assertion_id=request.supplements_assertion_id,
                    supersedes_assertion_id=request.supersedes_assertion_id,
                ),
            )
        except (ExperimentApplicationError, ValueError) as error:
            raise _error(error) from error
        return _experiment_view(stored, response)

    @app.post("/experiments/{experiment_id}/validations", status_code=status.HTTP_201_CREATED)
    def validate_route(
        experiment_id: str,
        request: ExpectedVersionRequest,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ValidationView:
        try:
            stored = service().run_validation(
                experiment_id,
                expected_version=request.expected_experiment_version,
                idempotency_key=_key(idempotency_key),
            )
        except (ExperimentApplicationError, ValueError) as error:
            raise _error(error) from error
        if stored.replayed:
            response.status_code = status.HTTP_200_OK
        return ValidationView(validation=stored.validation, replayed=stored.replayed)

    @app.get("/experiments/{experiment_id}/passport-preview")
    def preview_passport_route(experiment_id: str) -> PassportView:
        try:
            passport = service().preview_passport(experiment_id)
        except (ExperimentApplicationError, ValueError) as error:
            raise _error(error) from error
        return PassportView(passport=passport, replayed=False)

    @app.post("/experiments/{experiment_id}/passports", status_code=status.HTTP_201_CREATED)
    def release_passport_route(
        experiment_id: str,
        request: ExpectedVersionRequest,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> PassportView:
        try:
            stored = service().release_passport(
                experiment_id,
                expected_version=request.expected_experiment_version,
                idempotency_key=_key(idempotency_key),
            )
        except (ExperimentApplicationError, ValueError) as error:
            raise _error(error) from error
        return _passport_view(stored, response)

    @app.get("/experiment-passports/{passport_id}")
    def get_passport_route(passport_id: str) -> PassportView:
        try:
            passport = service().get_passport(passport_id)
        except ExperimentApplicationError as error:
            raise _error(error) from error
        return PassportView(passport=passport, replayed=True)

    @app.post("/experiments/{experiment_id}/packages", status_code=status.HTTP_201_CREATED)
    def create_package_route(
        experiment_id: str,
        request: CreatePackageRequest,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> PackageView:
        try:
            stored = service().create_package(
                experiment_id,
                passport_id=request.passport_id,
                expected_version=request.expected_experiment_version,
                idempotency_key=_key(idempotency_key),
            )
        except (ExperimentApplicationError, ValueError) as error:
            raise _error(error) from error
        return _package_view(stored, response)

    @app.get("/experiment-packages/{package_id}")
    def get_package_route(package_id: str) -> PackageView:
        try:
            package = service().get_package(package_id)
        except ExperimentApplicationError as error:
            raise _error(error) from error
        return PackageView(package=package, replayed=True)

    @app.get("/experiment-packages/{package_id}/download")
    def download_package_route(package_id: str) -> FastAPIResponse:
        try:
            archive_bytes = service().download_package(package_id)
        except (ExperimentApplicationError, ValueError) as error:
            raise _error(error) from error
        return FastAPIResponse(
            content=archive_bytes,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{package_id}.zip"',
            },
        )


__all__ = ["register_experiment_routes"]

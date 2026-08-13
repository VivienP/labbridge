"""Application use cases for versioned Experiments, Passports, and verified Packages."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field

from labbridge.application.cv_ingestion import StoredNormalisation, StoredProfile
from labbridge.application.source_intake import RetrievedSource
from labbridge.domain.canonical import canonical_bytes
from labbridge.domain.cv_observations import NormalisationResult, NormalisedSeries
from labbridge.domain.experiments import (
    AssertionTransformation,
    AssertionValue,
    Experiment,
    ExperimentVersionConflictError,
    MetadataAssertion,
    RequirementClass,
    ValidationRun,
    add_user_assertion,
    create_experiment,
    experiment_id_for_observation,
    make_assertion,
    validate_experiment,
)
from labbridge.domain.idempotency import normalise_idempotency_key
from labbridge.evidence.experiment_package import (
    ExperimentPackage,
    PackageInputs,
    build_experiment_package,
    verify_experiment_package,
)
from labbridge.evidence.manifest import digest
from labbridge.evidence.passport import ExperimentPassport, build_passport

VALIDATION_VERSION = "1"


class ExperimentApplicationError(Exception):
    code: ClassVar[str] = "experiment_application_error"


class ExperimentNotFoundError(ExperimentApplicationError):
    code = "experiment_not_found"

    def __init__(self, experiment_id: str) -> None:
        super().__init__(f"experiment `{experiment_id}` does not exist")


class PassportNotFoundError(ExperimentApplicationError):
    code = "experiment_passport_not_found"

    def __init__(self, passport_id: str) -> None:
        super().__init__(f"Experiment Passport `{passport_id}` does not exist")


class PackageNotFoundError(ExperimentApplicationError):
    code = "experiment_package_not_found"

    def __init__(self, package_id: str) -> None:
        super().__init__(f"Experiment Package `{package_id}` does not exist")


class ExperimentIdempotencyConflictError(ExperimentApplicationError):
    code = "experiment_idempotency_key_reused"

    def __init__(self, idempotency_key: str) -> None:
        super().__init__(f"idempotency key `{idempotency_key}` was reused for a different request")


class UserAssertionCommand(BaseModel):
    """A user edit with no client-selectable origin field."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field_name: str = Field(min_length=1)
    requirement_class: RequirementClass
    transformation: AssertionTransformation
    value: AssertionValue
    evidence_note: str = Field(min_length=1)
    supplements_assertion_id: str | None = None
    supersedes_assertion_id: str | None = None


class NormalisationReader(Protocol):
    def get_normalisation(self, observation_id: str) -> StoredNormalisation: ...

    def get_profile(self, profile_id: str) -> StoredProfile: ...


class SourceReader(Protocol):
    def retrieve(self, source_artifact_id: str) -> RetrievedSource: ...


class ExperimentRepository(Protocol):
    def create(
        self, experiment: Experiment, *, idempotency_key: str, request_hash: str
    ) -> tuple[Experiment, bool]: ...

    def get(self, experiment_id: str) -> Experiment | None: ...

    def append(
        self,
        experiment: Experiment,
        *,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[Experiment, bool]: ...

    def store_validation(
        self,
        validation: ValidationRun,
        *,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ValidationRun, bool]: ...

    def store_passport(
        self,
        passport: ExperimentPassport,
        *,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ExperimentPassport, bool]: ...

    def latest_passport(self, experiment_id: str) -> ExperimentPassport | None: ...

    def get_passport(self, passport_id: str) -> ExperimentPassport | None: ...

    def store_package(
        self,
        package: ExperimentPackage,
        archive_bytes: bytes,
        *,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ExperimentPackage, bool]: ...

    def latest_package(self, experiment_id: str) -> ExperimentPackage | None: ...

    def get_package(self, package_id: str) -> tuple[ExperimentPackage, bytes] | None: ...


@dataclass(frozen=True)
class StoredExperiment:
    experiment: Experiment
    replayed: bool


@dataclass(frozen=True)
class StoredValidation:
    validation: ValidationRun
    replayed: bool


@dataclass(frozen=True)
class StoredPassport:
    passport: ExperimentPassport
    replayed: bool


@dataclass(frozen=True)
class StoredPackage:
    package: ExperimentPackage
    replayed: bool


_METADATA_REQUIREMENTS: Mapping[str, RequirementClass] = {
    "reference_scale": "conditional",
    "potential_treatment": "conditional",
    "current_basis": "conditional",
    "electrode_role": "conditional",
    "geometric_area": "conditional",
    "contact_area": "conditional",
    "scan_rate": "recommended",
    "cycle_information": "recommended",
}


def _axis_assertion(
    experiment_id: str,
    result: NormalisationResult,
    series: NormalisedSeries,
) -> MetadataAssertion:
    field_name = "potential_axis" if series.role == "potential" else "current_axis"
    parser_evidence = (
        ()
        if result.observation.parser_record_id is None
        else (result.observation.parser_record_id,)
    )
    return make_assertion(
        experiment_id=experiment_id,
        field_name=field_name,
        requirement_class="required",
        origin="user_supplied",
        transformation=("parsed" if series.source_unit == series.unit else "unit_converted"),
        value=AssertionValue(
            state="known",
            value=series.source_column,
            unit=series.unit,
        ),
        evidence_ids=(
            result.observation.source_artifact_id,
            result.observation.import_profile_id,
            *parser_evidence,
            series.transformation_id,
        ),
        evidence_note=(
            "The explicit Phase 2 import profile assigns this source column, role, and unit."
        ),
    )


def _initial_assertions(result: NormalisationResult) -> tuple[MetadataAssertion, ...]:
    observation = result.observation
    experiment_id = experiment_id_for_observation(observation.observation_id)
    parser_evidence = (
        () if observation.parser_record_id is None else (observation.parser_record_id,)
    )
    assertions: list[MetadataAssertion] = [
        make_assertion(
            experiment_id=experiment_id,
            field_name="source_artifact",
            requirement_class="required",
            origin="source_file",
            transformation="none",
            value=AssertionValue(state="known", value=observation.source_artifact_id),
            evidence_ids=(observation.source_artifact_id,),
            evidence_note="Exact Phase 1 source bytes are retained under this identity.",
        ),
        make_assertion(
            experiment_id=experiment_id,
            field_name="observation",
            requirement_class="required",
            origin="source_file",
            transformation="derived",
            value=AssertionValue(state="known", value=observation.observation_id),
            evidence_ids=(
                observation.source_artifact_id,
                *parser_evidence,
                *observation.transformation_ids,
            ),
            evidence_note="The Phase 2 transformation graph closes this observation to the source.",
        ),
    ]
    if result.parser_record is not None:
        assertions.append(
            make_assertion(
                experiment_id=experiment_id,
                field_name="parser_record",
                requirement_class="required",
                origin="source_file",
                transformation="parsed",
                value=AssertionValue(state="known", value=result.parser_record.parser_record_id),
                evidence_ids=(
                    observation.source_artifact_id,
                    result.parser_record.parser_record_id,
                    observation.transformation_ids[0],
                ),
                evidence_note=(
                    "The accepted parser record identifies the supported DTA variant and exact "
                    "source field locations."
                ),
            )
        )
    assertions.extend(
        _axis_assertion(experiment_id, result, series)
        for series in observation.series
        if series.role in {"potential", "current", "current_density"}
    )
    for field_name, requirement_class in _METADATA_REQUIREMENTS.items():
        metadata_value = getattr(observation.metadata, field_name)
        assertions.append(
            make_assertion(
                experiment_id=experiment_id,
                field_name=field_name,
                requirement_class=requirement_class,
                origin="user_supplied",
                transformation="none",
                value=AssertionValue(
                    state=metadata_value.state,
                    value=metadata_value.value,
                    unit=metadata_value.unit,
                ),
                evidence_ids=(
                    observation.source_artifact_id,
                    observation.import_profile_id,
                    *parser_evidence,
                    *observation.transformation_ids,
                ),
                evidence_note=(
                    "The user-supplied Phase 2 profile declares this metadata state without "
                    "inferring context from the source file."
                ),
            )
        )
    return tuple(assertions)


def experiment_from_normalisation(result: NormalisationResult) -> Experiment:
    """Create the initial aggregate without changing any Phase 1-2 identity."""
    observation = result.observation
    return create_experiment(
        observation_id=observation.observation_id,
        source_artifact_id=observation.source_artifact_id,
        import_profile_id=observation.import_profile_id,
        technique="cyclic_voltammetry",
        data_origin=observation.data_origin,
        execution_mode=observation.execution_mode,
        environment_id=observation.environment_id,
        transformation_ids=observation.transformation_ids,
        assertions=_initial_assertions(result),
    )


def _request_hash(payload: object) -> str:
    return digest(canonical_bytes(payload))


class ExperimentService:
    """The shared application boundary used by HTTP, CLI, and artifact reproduction."""

    def __init__(
        self,
        normalisations: NormalisationReader,
        sources: SourceReader,
        repository: ExperimentRepository,
        *,
        clock: Callable[[], datetime],
        producing_versions: Mapping[str, str],
    ) -> None:
        self._normalisations = normalisations
        self._sources = sources
        self._repository = repository
        self._clock = clock
        self._producing_versions = dict(producing_versions)

    def _key(self, key: str) -> str:
        return normalise_idempotency_key(key)

    def _experiment(self, experiment_id: str) -> Experiment:
        experiment = self._repository.get(experiment_id)
        if experiment is None:
            raise ExperimentNotFoundError(experiment_id)
        return experiment

    def create_experiment(
        self,
        observation_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> StoredExperiment:
        if expected_version != 0:
            raise ValueError("a new experiment requires expected experiment version 0")
        stored = self._normalisations.get_normalisation(observation_id)
        experiment = experiment_from_normalisation(stored.result)
        result, replayed = self._repository.create(
            experiment,
            idempotency_key=self._key(idempotency_key),
            request_hash=_request_hash(
                {"observation_id": observation_id, "expected_version": expected_version}
            ),
        )
        return StoredExperiment(result, replayed)

    def get_experiment(self, experiment_id: str) -> StoredExperiment:
        return StoredExperiment(self._experiment(experiment_id), True)

    def add_user_assertion(
        self,
        experiment_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        command: UserAssertionCommand,
    ) -> StoredExperiment:
        current = self._experiment(experiment_id)
        next_version = add_user_assertion(
            current,
            expected_version=expected_version,
            field_name=command.field_name,
            requirement_class=command.requirement_class,
            transformation=command.transformation,
            value=command.value,
            evidence_note=command.evidence_note,
            supplements_assertion_id=command.supplements_assertion_id,
            supersedes_assertion_id=command.supersedes_assertion_id,
        )
        result, replayed = self._repository.append(
            next_version,
            expected_version=expected_version,
            idempotency_key=self._key(idempotency_key),
            request_hash=_request_hash(
                {
                    "experiment_id": experiment_id,
                    "expected_version": expected_version,
                    "command": command,
                }
            ),
        )
        return StoredExperiment(result, replayed)

    def run_validation(
        self,
        experiment_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> StoredValidation:
        experiment = self._experiment(experiment_id)
        if experiment.version != expected_version:
            raise ExperimentVersionConflictError(expected_version, experiment.version)
        validation = validate_experiment(experiment, validation_version=VALIDATION_VERSION)
        result, replayed = self._repository.store_validation(
            validation,
            expected_version=expected_version,
            idempotency_key=self._key(idempotency_key),
            request_hash=_request_hash(
                {"experiment_id": experiment_id, "expected_version": expected_version}
            ),
        )
        return StoredValidation(result, replayed)

    def preview_passport(self, experiment_id: str) -> ExperimentPassport:
        experiment = self._experiment(experiment_id)
        validation = validate_experiment(experiment, validation_version=VALIDATION_VERSION)
        return build_passport(
            experiment,
            validation,
            released_at=None,
            release=False,
            supersedes_passport_id=None,
        )

    def get_passport(self, passport_id: str) -> ExperimentPassport:
        passport = self._repository.get_passport(passport_id)
        if passport is None:
            raise PassportNotFoundError(passport_id)
        return passport

    def release_passport(
        self,
        experiment_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> StoredPassport:
        experiment = self._experiment(experiment_id)
        if experiment.version != expected_version:
            raise ExperimentVersionConflictError(expected_version, experiment.version)
        validation = validate_experiment(experiment, validation_version=VALIDATION_VERSION)
        key = self._key(idempotency_key)
        previous = self._repository.latest_passport(experiment_id)
        passport = build_passport(
            experiment,
            validation,
            released_at=self._clock(),
            release=True,
            supersedes_passport_id=(previous.passport_id if previous is not None else None),
        )
        result, replayed = self._repository.store_passport(
            passport,
            expected_version=expected_version,
            idempotency_key=key,
            request_hash=_request_hash(
                {"experiment_id": experiment_id, "expected_version": expected_version}
            ),
        )
        return StoredPassport(result, replayed)

    def create_package(
        self,
        experiment_id: str,
        *,
        passport_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> StoredPackage:
        experiment = self._experiment(experiment_id)
        if experiment.version != expected_version:
            raise ExperimentVersionConflictError(expected_version, experiment.version)
        passport = self._repository.get_passport(passport_id)
        if passport is None or passport.experiment_id != experiment_id:
            raise PassportNotFoundError(passport_id)
        if passport.experiment_version != expected_version:
            raise ValueError("Passport does not match the expected experiment version")
        normalisation = self._normalisations.get_normalisation(experiment.observation_id).result
        profile = self._normalisations.get_profile(experiment.import_profile_id).profile
        source = self._sources.retrieve(experiment.source_artifact_id)
        previous = self._repository.latest_package(experiment_id)
        producing_versions = dict(self._producing_versions)
        if normalisation.parser_record is not None:
            producing_versions["experiment_package"] = "2"
            producing_versions["gamry_dta_parser"] = normalisation.parser_record.parser_version
        built = build_experiment_package(
            PackageInputs(
                source_filename=source.artifact.filename,
                source_bytes=source.data,
                source_artifact=source.artifact.model_dump(mode="json"),
                import_profile={
                    "profile_id": experiment.import_profile_id,
                    **profile.model_dump(mode="json"),
                },
                normalised_observation=normalisation.observation.model_dump(mode="json"),
                transformation_graph=normalisation.graph.model_dump(mode="json"),
                parser_record=(
                    None
                    if normalisation.parser_record is None
                    else normalisation.parser_record.model_dump(mode="json")
                ),
                passport=passport,
            ),
            producing_versions=producing_versions,
            supersedes_package_id=(previous.package_id if previous is not None else None),
        )
        result, replayed = self._repository.store_package(
            built.metadata,
            built.archive_bytes,
            expected_version=expected_version,
            idempotency_key=self._key(idempotency_key),
            request_hash=_request_hash(
                {
                    "experiment_id": experiment_id,
                    "passport_id": passport_id,
                    "expected_version": expected_version,
                }
            ),
        )
        return StoredPackage(result, replayed)

    def download_package(self, package_id: str) -> bytes:
        stored = self._repository.get_package(package_id)
        if stored is None:
            raise PackageNotFoundError(package_id)
        metadata, archive_bytes = stored
        if digest(archive_bytes) != metadata.archive_sha256:
            raise ValueError("retained Experiment Package no longer matches its checksum")
        verification = verify_experiment_package(archive_bytes)
        if verification.package_id != metadata.package_id:
            raise ValueError("retained Experiment Package no longer matches its identity")
        return archive_bytes

    def get_package(self, package_id: str) -> ExperimentPackage:
        stored = self._repository.get_package(package_id)
        if stored is None:
            raise PackageNotFoundError(package_id)
        return stored[0]


__all__ = [
    "ExperimentApplicationError",
    "ExperimentIdempotencyConflictError",
    "ExperimentNotFoundError",
    "ExperimentRepository",
    "ExperimentService",
    "ExperimentVersionConflictError",
    "PackageNotFoundError",
    "PassportNotFoundError",
    "StoredExperiment",
    "StoredPackage",
    "StoredPassport",
    "StoredValidation",
    "UserAssertionCommand",
    "experiment_from_normalisation",
]

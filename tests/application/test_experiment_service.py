from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from labbridge.application.experiments import (
    ExperimentIdempotencyConflictError,
    ExperimentService,
    UserAssertionCommand,
)
from labbridge.application.source_intake import RetrievedSource
from labbridge.domain.cv import CVImportProfile, import_profile_id
from labbridge.domain.cv_observations import NormalisationResult
from labbridge.domain.experiments import AssertionValue
from labbridge.domain.source_artifacts import SourceArtifact
from labbridge.evidence.experiment_package import verify_experiment_package

ROOT = Path(__file__).resolve().parents[2]
PHASE_2 = ROOT / "artifacts/cv-ingestion"
PHASE_1 = ROOT / "artifacts/source-capture"
SUPERSEDING_VERSION = 2


class MemoryExperimentRepository:
    def __init__(self) -> None:
        self.experiments: dict[str, object] = {}
        self.validations: dict[str, object] = {}
        self.passports: dict[str, object] = {}
        self.packages: dict[str, tuple[object, bytes]] = {}
        self.keys: dict[tuple[str, str], tuple[str, object]] = {}

    def _store(self, scope: str, key: str, request_hash: str, value: object) -> tuple[object, bool]:
        existing = self.keys.get((scope, key))
        if existing is not None:
            if existing[0] != request_hash:
                raise ExperimentIdempotencyConflictError(key)
            return existing[1], True
        self.keys[(scope, key)] = (request_hash, value)
        return value, False

    def create(self, experiment, *, idempotency_key: str, request_hash: str):
        stored, replayed = self._store(
            "experiment.create", idempotency_key, request_hash, experiment
        )
        self.experiments[stored.experiment_id] = stored
        return stored, replayed

    def get(self, experiment_id: str):
        return self.experiments.get(experiment_id)

    def append(self, experiment, *, expected_version: int, idempotency_key: str, request_hash: str):
        current = self.experiments[experiment.experiment_id]
        if current.version != expected_version:
            raise ValueError(
                f"expected experiment version {expected_version}, found {current.version}"
            )
        stored, replayed = self._store(
            f"experiment.assertion:{experiment.experiment_id}",
            idempotency_key,
            request_hash,
            experiment,
        )
        self.experiments[stored.experiment_id] = stored
        return stored, replayed

    def store_validation(
        self, validation, *, expected_version: int, idempotency_key: str, request_hash: str
    ):
        assert validation.experiment_version == expected_version
        stored, replayed = self._store(
            f"experiment.validation:{validation.experiment_id}",
            idempotency_key,
            request_hash,
            validation,
        )
        self.validations[stored.validation_id] = stored
        return stored, replayed

    def store_passport(
        self, passport, *, expected_version: int, idempotency_key: str, request_hash: str
    ):
        assert passport.experiment_version == expected_version
        stored, replayed = self._store(
            f"experiment.passport:{passport.experiment_id}",
            idempotency_key,
            request_hash,
            passport,
        )
        self.passports[stored.passport_id] = stored
        return stored, replayed

    def latest_passport(self, experiment_id: str):
        matches = [item for item in self.passports.values() if item.experiment_id == experiment_id]
        return max(matches, key=lambda item: item.experiment_version, default=None)

    def get_passport(self, passport_id: str):
        return self.passports.get(passport_id)

    def store_package(
        self,
        package,
        archive_bytes: bytes,
        *,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
    ):
        assert package.experiment_version == expected_version
        stored, replayed = self._store(
            f"experiment.package:{package.experiment_id}",
            idempotency_key,
            request_hash,
            package,
        )
        self.packages[stored.package_id] = (stored, archive_bytes)
        return stored, replayed

    def latest_package(self, experiment_id: str):
        matches = [
            item[0] for item in self.packages.values() if item[0].experiment_id == experiment_id
        ]
        return max(matches, key=lambda item: item.experiment_version, default=None)

    def get_package(self, package_id: str):
        return self.packages.get(package_id)


class Phase2Reader:
    def __init__(self, result: NormalisationResult, profile: CVImportProfile) -> None:
        self.result = result
        self.profile = profile

    def get_normalisation(self, observation_id: str):
        if observation_id != self.result.observation.observation_id:
            raise LookupError(observation_id)
        return type("Stored", (), {"result": self.result, "replayed": True})()

    def get_profile(self, profile_id: str):
        if profile_id != import_profile_id(self.profile):
            raise LookupError(profile_id)
        return type(
            "StoredProfile",
            (),
            {"profile_id": profile_id, "profile": self.profile, "replayed": True},
        )()


class Phase1Reader:
    def __init__(self, source: RetrievedSource) -> None:
        self.source = source

    def retrieve(self, source_artifact_id: str) -> RetrievedSource:
        if source_artifact_id != self.source.artifact.source_artifact_id:
            raise LookupError(source_artifact_id)
        return self.source


def _source() -> RetrievedSource:
    record = json.loads((PHASE_1 / "source-artifact.json").read_text(encoding="utf-8"))
    record.pop("schema_version")
    timestamp = datetime(2026, 8, 12, tzinfo=UTC)
    artifact = SourceArtifact(
        **record,
        created_at=timestamp,
        committed_at=timestamp,
    )
    return RetrievedSource(
        artifact=artifact,
        data=(PHASE_1 / record["filename"]).read_bytes(),
    )


@pytest.fixture
def service() -> ExperimentService:
    result = NormalisationResult.model_validate_json(
        json.dumps(
            {
                "observation": json.loads(
                    (PHASE_2 / "normalised-observation.json").read_text(encoding="utf-8")
                ),
                "graph": json.loads(
                    (PHASE_2 / "transformation-graph.json").read_text(encoding="utf-8")
                ),
                "findings": json.loads(
                    (PHASE_2 / "structural-findings.json").read_text(encoding="utf-8")
                ),
            }
        )
    )
    profile = CVImportProfile.model_validate_json(
        (PHASE_2 / "import-profile.json").read_text(encoding="utf-8")
    )
    return ExperimentService(
        Phase2Reader(result, profile),
        Phase1Reader(_source()),
        MemoryExperimentRepository(),
        clock=lambda: datetime(2026, 8, 12, 20, 0, tzinfo=UTC),
        producing_versions={"labbridge": "0.1.0", "experiment_package": "1"},
    )


def test_create_replays_same_request(service: ExperimentService) -> None:
    observation_id = "cv-observation:6b9846ff3dfe2a38e2989984c21d450a"

    created = service.create_experiment(
        observation_id, expected_version=0, idempotency_key="create-phase3"
    )
    replayed = service.create_experiment(
        observation_id, expected_version=0, idempotency_key="create-phase3"
    )

    assert created.replayed is False
    assert replayed.replayed is True
    assert replayed.experiment == created.experiment


def test_profile_metadata_origin_is_independent_from_value_state(
    service: ExperimentService,
) -> None:
    experiment = service.create_experiment(
        "cv-observation:6b9846ff3dfe2a38e2989984c21d450a",
        expected_version=0,
        idempotency_key="create-origin-contract",
    ).experiment
    metadata = {
        item.field_name: item
        for item in experiment.assertions
        if item.field_name
        in {
            "reference_scale",
            "potential_treatment",
            "current_basis",
            "electrode_role",
            "geometric_area",
            "contact_area",
            "scan_rate",
            "cycle_information",
        }
    }

    assert {item.origin for item in metadata.values()} == {"user_supplied"}
    assert {item.transformation for item in metadata.values()} == {"none"}
    assert {item.value.state for item in metadata.values()} == {
        "known",
        "unknown",
        "unavailable",
        "not_applicable",
    }


def test_reused_mutation_key_with_different_body_conflicts(service: ExperimentService) -> None:
    created = service.create_experiment(
        "cv-observation:6b9846ff3dfe2a38e2989984c21d450a",
        expected_version=0,
        idempotency_key="create",
    )
    experiment = created.experiment
    profile_assertion = next(
        item for item in experiment.assertions if item.field_name == "reference_scale"
    )
    first = UserAssertionCommand(
        field_name="reference_scale",
        requirement_class="conditional",
        transformation="none",
        value=AssertionValue(state="known", value="RHE"),
        evidence_note="First declaration.",
        supplements_assertion_id=profile_assertion.assertion_id,
    )
    service.add_user_assertion(
        experiment.experiment_id,
        expected_version=1,
        idempotency_key="same-key",
        command=first,
    )

    with pytest.raises(ExperimentIdempotencyConflictError):
        service.add_user_assertion(
            experiment.experiment_id,
            expected_version=2,
            idempotency_key="same-key",
            command=first.model_copy(
                update={
                    "value": AssertionValue(state="known", value="Ag/AgCl"),
                    "evidence_note": "Different declaration.",
                }
            ),
        )


def test_service_releases_initial_and_superseding_immutable_packages(
    service: ExperimentService,
) -> None:
    created = service.create_experiment(
        "cv-observation:6b9846ff3dfe2a38e2989984c21d450a",
        expected_version=0,
        idempotency_key="create",
    )
    experiment_id = created.experiment.experiment_id
    initial_validation = service.run_validation(
        experiment_id, expected_version=1, idempotency_key="validate-v1"
    )
    initial_passport = service.release_passport(
        experiment_id, expected_version=1, idempotency_key="passport-v1"
    )
    initial_package = service.create_package(
        experiment_id,
        passport_id=initial_passport.passport.passport_id,
        expected_version=1,
        idempotency_key="package-v1",
    )
    original_bytes = service.download_package(initial_package.package.package_id)

    profile_assertion = next(
        item
        for item in created.experiment.assertions
        if item.field_name == "reference_scale" and item.origin == "user_supplied"
    )
    source_assertion = next(
        item
        for item in created.experiment.assertions
        if item.field_name == "source_artifact" and item.origin == "source_file"
    )
    supplemented = service.add_user_assertion(
        experiment_id,
        expected_version=1,
        idempotency_key="assert-v2",
        command=UserAssertionCommand(
            field_name="reference_scale",
            requirement_class="conditional",
            transformation="none",
            value=AssertionValue(state="known", value="RHE"),
            evidence_note="Operator declaration for this experiment.",
            supplements_assertion_id=profile_assertion.assertion_id,
        ),
    )
    superseding_passport = service.release_passport(
        experiment_id, expected_version=2, idempotency_key="passport-v2"
    )
    superseding_package = service.create_package(
        experiment_id,
        passport_id=superseding_passport.passport.passport_id,
        expected_version=2,
        idempotency_key="package-v2",
    )

    assert initial_validation.validation.release_decision.status == "eligible"
    assert supplemented.experiment.version == SUPERSEDING_VERSION
    assert source_assertion in supplemented.experiment.assertions
    assert service.download_package(initial_package.package.package_id) == original_bytes
    assert (
        superseding_passport.passport.supersedes_passport_id
        == initial_passport.passport.passport_id
    )
    assert superseding_package.package.supersedes_package_id == initial_package.package.package_id
    assert verify_experiment_package(original_bytes).experiment_version == 1
    assert (
        verify_experiment_package(
            service.download_package(superseding_package.package.package_id)
        ).experiment_version
        == SUPERSEDING_VERSION
    )

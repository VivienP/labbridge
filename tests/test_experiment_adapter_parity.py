from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import status
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from labbridge.api.app import create_app
from labbridge.application.experiments import (
    ExperimentIdempotencyConflictError,
    StoredPackage,
    StoredPassport,
    StoredValidation,
)
from labbridge.cli import app
from labbridge.domain.experiments import (
    AssertionValue,
    create_experiment,
    experiment_id_for_observation,
    make_assertion,
    validate_experiment,
)
from labbridge.evidence.experiment_package import ExperimentPackage
from labbridge.evidence.passport import build_passport

SOURCE_ID = "source:97fbafab40e0dde53c902a88f32e5088"
OBSERVATION_ID = "cv-observation:6b9846ff3dfe2a38e2989984c21d450a"
TRANSFORM_ID = "transform:e5c8b9c23624b210ebbd534fc1183fe7"


def _experiment():
    experiment_id = experiment_id_for_observation(OBSERVATION_ID)
    assertions = tuple(
        make_assertion(
            experiment_id=experiment_id,
            field_name=field_name,
            requirement_class="required",
            origin="source_file",
            transformation="parsed",
            value=AssertionValue(state="known", value=value, unit=unit),
            evidence_ids=(SOURCE_ID, TRANSFORM_ID),
            evidence_note="Retained Phase 1 and Phase 2 evidence.",
        )
        for field_name, value, unit in (
            ("source_artifact", SOURCE_ID, None),
            ("observation", OBSERVATION_ID, None),
            ("potential_axis", "channel_a", "V"),
            ("current_axis", "channel_b", "A"),
        )
    )
    return create_experiment(
        observation_id=OBSERVATION_ID,
        source_artifact_id=SOURCE_ID,
        import_profile_id="cv-profile:93b2ab987c861b65a4284876505bd8d5",
        technique="cyclic_voltammetry",
        data_origin="synthetic",
        execution_mode="replay",
        environment_id="synthetic_cv_fixture",
        transformation_ids=(TRANSFORM_ID,),
        assertions=assertions,
    )


class ValidationService:
    def __init__(self) -> None:
        self.experiment = _experiment()
        self.passport = build_passport(
            self.experiment,
            validate_experiment(self.experiment, validation_version="1"),
            released_at=datetime(2026, 8, 12, 20, 0, tzinfo=UTC),
            release=True,
        )
        self.package = ExperimentPackage(
            package_id="experiment-package:parity",
            schema_version="1",
            passport_id=self.passport.passport_id,
            experiment_id=self.experiment.experiment_id,
            experiment_version=1,
            data_origin="synthetic",
            execution_mode="replay",
            environment_id="synthetic_cv_fixture",
            archive_sha256="0" * 64,
            archive_byte_size=1,
            producing_versions={"experiment_package": "1", "labbridge": "0.1.0"},
        )

    def run_validation(
        self, experiment_id: str, *, expected_version: int, idempotency_key: str
    ) -> StoredValidation:
        assert experiment_id == self.experiment.experiment_id
        assert expected_version == 1
        assert idempotency_key in {
            "parity-validation",
            "ui-validation",
            "cli-validation-new-key",
        }
        return StoredValidation(
            validate_experiment(self.experiment, validation_version="1"), replayed=False
        )

    def release_passport(
        self, experiment_id: str, *, expected_version: int, idempotency_key: str
    ) -> StoredPassport:
        assert experiment_id == self.experiment.experiment_id
        assert expected_version == 1
        assert idempotency_key in {
            "parity-passport",
            "ui-passport",
            "cli-passport-new-key",
        }
        return StoredPassport(self.passport, replayed=False)

    def create_package(
        self,
        experiment_id: str,
        *,
        passport_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> StoredPackage:
        assert experiment_id == self.experiment.experiment_id
        assert passport_id == self.passport.passport_id
        assert expected_version == 1
        assert idempotency_key in {
            "parity-package",
            "ui-package",
            "cli-package-new-key",
        }
        return StoredPackage(self.package, replayed=False)


class ConflictingPackageService(ValidationService):
    def create_package(
        self,
        experiment_id: str,
        *,
        passport_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> StoredPackage:
        raise ExperimentIdempotencyConflictError(idempotency_key)


def test_api_and_cli_render_the_same_validation_contract(monkeypatch) -> None:
    service = ValidationService()
    client = TestClient(create_app(experiment_service=service))  # type: ignore[arg-type]
    monkeypatch.setattr("labbridge.cli._build_experiment_service", lambda: service)

    api_response = client.post(
        f"/experiments/{service.experiment.experiment_id}/validations",
        headers={"Idempotency-Key": "parity-validation"},
        json={"expected_experiment_version": 1},
    )
    cli_response = CliRunner().invoke(
        app,
        [
            "experiment",
            "validate",
            service.experiment.experiment_id,
            "--expected-version",
            "1",
            "--idempotency-key",
            "parity-validation",
            "--json",
        ],
    )

    assert api_response.status_code == status.HTTP_201_CREATED
    assert cli_response.exit_code == 0
    assert json.loads(cli_response.stdout) == api_response.json()


def test_api_and_cli_render_the_same_passport_identity_and_release_decision(monkeypatch) -> None:
    service = ValidationService()
    client = TestClient(create_app(experiment_service=service))  # type: ignore[arg-type]
    monkeypatch.setattr("labbridge.cli._build_experiment_service", lambda: service)

    api_response = client.post(
        f"/experiments/{service.experiment.experiment_id}/passports",
        headers={"Idempotency-Key": "parity-passport"},
        json={"expected_experiment_version": 1},
    )
    cli_response = CliRunner().invoke(
        app,
        [
            "experiment",
            "passport-release",
            service.experiment.experiment_id,
            "--expected-version",
            "1",
            "--idempotency-key",
            "parity-passport",
            "--json",
        ],
    )

    assert api_response.status_code == status.HTTP_201_CREATED
    assert cli_response.exit_code == 0
    assert json.loads(cli_response.stdout) == api_response.json()


def test_api_and_cli_render_the_same_package_identity_and_checksum(monkeypatch) -> None:
    service = ValidationService()
    client = TestClient(create_app(experiment_service=service))  # type: ignore[arg-type]
    monkeypatch.setattr("labbridge.cli._build_experiment_service", lambda: service)

    api_response = client.post(
        f"/experiments/{service.experiment.experiment_id}/packages",
        headers={"Idempotency-Key": "parity-package"},
        json={
            "expected_experiment_version": 1,
            "passport_id": service.passport.passport_id,
        },
    )
    cli_response = CliRunner().invoke(
        app,
        [
            "package",
            "create",
            service.experiment.experiment_id,
            "--passport-id",
            service.passport.passport_id,
            "--expected-version",
            "1",
            "--idempotency-key",
            "parity-package",
            "--json",
        ],
    )

    assert api_response.status_code == status.HTTP_201_CREATED
    assert cli_response.exit_code == 0
    assert json.loads(cli_response.stdout) == api_response.json()


def test_user_assertion_http_contract_rejects_client_selected_origin() -> None:
    client = TestClient(create_app(experiment_service=ValidationService()))  # type: ignore[arg-type]

    response = client.post(
        f"/experiments/{_experiment().experiment_id}/assertions",
        headers={"Idempotency-Key": "protected-origin"},
        json={
            "expected_experiment_version": 1,
            "field_name": "reference_scale",
            "requirement_class": "conditional",
            "origin": "source_file",
            "transformation": "none",
            "value": {"state": "known", "value": "RHE", "unit": None},
            "evidence_note": "Attempted protected origin.",
        },
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert any(error["loc"][-1] == "origin" for error in response.json()["detail"])


def test_package_changed_content_idempotency_conflict_is_http_409() -> None:
    service = ConflictingPackageService()
    client = TestClient(create_app(experiment_service=service))  # type: ignore[arg-type]

    response = client.post(
        f"/experiments/{service.experiment.experiment_id}/packages",
        headers={"Idempotency-Key": "package-key-reused-for-new-version"},
        json={
            "expected_experiment_version": 2,
            "passport_id": service.passport.passport_id,
        },
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["detail"]["code"] == "experiment_idempotency_key_reused"


def test_ui_api_and_cli_equivalent_inputs_converge_with_new_keys(monkeypatch) -> None:
    service = ValidationService()
    client = TestClient(create_app(experiment_service=service))  # type: ignore[arg-type]
    monkeypatch.setattr("labbridge.cli._build_experiment_service", lambda: service)
    runner = CliRunner()
    experiment_id = service.experiment.experiment_id

    ui_validation = client.post(
        f"/experiments/{experiment_id}/validations",
        headers={"Idempotency-Key": "ui-validation"},
        json={"expected_experiment_version": 1},
    ).json()
    cli_validation = json.loads(
        runner.invoke(
            app,
            [
                "experiment",
                "validate",
                experiment_id,
                "--expected-version",
                "1",
                "--idempotency-key",
                "cli-validation-new-key",
                "--json",
            ],
        ).stdout
    )

    ui_passport = client.post(
        f"/experiments/{experiment_id}/passports",
        headers={"Idempotency-Key": "ui-passport"},
        json={"expected_experiment_version": 1},
    ).json()
    cli_passport = json.loads(
        runner.invoke(
            app,
            [
                "experiment",
                "passport-release",
                experiment_id,
                "--expected-version",
                "1",
                "--idempotency-key",
                "cli-passport-new-key",
                "--json",
            ],
        ).stdout
    )

    passport_id = ui_passport["passport"]["passport_id"]
    ui_package = client.post(
        f"/experiments/{experiment_id}/packages",
        headers={"Idempotency-Key": "ui-package"},
        json={"expected_experiment_version": 1, "passport_id": passport_id},
    ).json()
    cli_package = json.loads(
        runner.invoke(
            app,
            [
                "package",
                "create",
                experiment_id,
                "--passport-id",
                passport_id,
                "--expected-version",
                "1",
                "--idempotency-key",
                "cli-package-new-key",
                "--json",
            ],
        ).stdout
    )

    assert [item["finding_id"] for item in ui_validation["validation"]["findings"]] == [
        item["finding_id"] for item in cli_validation["validation"]["findings"]
    ]
    assert cli_passport["passport"]["passport_id"] == passport_id
    assert (
        cli_passport["passport"]["release_decision"] == ui_passport["passport"]["release_decision"]
    )
    assert cli_package["package"]["package_id"] == ui_package["package"]["package_id"]
    assert cli_package["package"]["archive_sha256"] == ui_package["package"]["archive_sha256"]

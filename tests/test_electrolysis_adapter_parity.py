from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from electrolysis_helpers import (
    FixedElectrolysisSourceReader,
    MemoryElectrolysisRecords,
    electrolysis_profile,
    electrolysis_source,
)
from labbridge import cli
from labbridge.api.app import create_app
from labbridge.application.electrolysis_ingestion import ElectrolysisIngestionService

runner = CliRunner()
OK = 200
CREATED = 201
BAD_REQUEST = 400
CONFLICT = 409
NOT_FOUND = 404


def _service() -> ElectrolysisIngestionService:
    return ElectrolysisIngestionService(
        FixedElectrolysisSourceReader(),
        MemoryElectrolysisRecords(),
        producing_version="0.1.0",
    )


def test_api_and_cli_return_the_same_electrolysis_profile_and_observation(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    service = _service()
    client = TestClient(create_app(electrolysis_service=service))
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(electrolysis_profile().model_dump_json(indent=2), encoding="utf-8")

    api_profile = client.post(
        "/electrolysis/import-profiles",
        json=electrolysis_profile().model_dump(mode="json"),
        headers={"Idempotency-Key": "electrolysis-profile-1"},
    )
    assert api_profile.status_code == CREATED
    profile_id = api_profile.json()["profile_id"]
    api_normalised = client.post(
        "/electrolysis/normalisations",
        json={
            "source_artifact_id": electrolysis_source().artifact.source_artifact_id,
            "profile_id": profile_id,
        },
        headers={"Idempotency-Key": "electrolysis-normalise-1"},
    )
    assert api_normalised.status_code == CREATED
    observation = api_normalised.json()["result"]["observation"]
    observation_id = observation["observation_id"]
    api_read = client.get(f"/electrolysis/normalised-observations/{observation_id}")
    assert api_read.status_code == OK

    monkeypatch.setattr(cli, "_build_electrolysis_service", lambda: service)
    cli_profile = runner.invoke(
        cli.app, ["electrolysis", "profile-create", str(profile_path), "--json"]
    )
    assert cli_profile.exit_code == 0, cli_profile.output
    cli_normalised = runner.invoke(
        cli.app,
        [
            "electrolysis",
            "normalise",
            electrolysis_source().artifact.source_artifact_id,
            "--profile-id",
            profile_id,
            "--json",
        ],
    )
    assert cli_normalised.exit_code == 0, cli_normalised.output

    assert json.loads(cli_normalised.output)["result"] == api_normalised.json()["result"]
    assert api_read.json()["result"] == api_normalised.json()["result"]
    assert observation["technique"] == "galvanostatic_electrolysis"
    assert observation["metadata"]["chemical_analysis"]["state"] == "unavailable"
    assert "faradaic" not in json.dumps(observation).lower()
    assert "selectivity" not in json.dumps(observation).lower()
    assert "yield" not in json.dumps(observation).lower()


def test_mutating_electrolysis_endpoints_require_an_idempotency_key() -> None:
    client = TestClient(create_app(electrolysis_service=_service()))

    profile_response = client.post(
        "/electrolysis/import-profiles", json=electrolysis_profile().model_dump(mode="json")
    )
    normalise_response = client.post(
        "/electrolysis/normalisations",
        json={
            "source_artifact_id": electrolysis_source().artifact.source_artifact_id,
            "profile_id": "electrolysis-profile:any",
        },
    )

    assert profile_response.status_code == BAD_REQUEST
    assert profile_response.json()["detail"]["code"] == "idempotency_key_required"
    assert normalise_response.status_code == BAD_REQUEST
    assert normalise_response.json()["detail"]["code"] == "idempotency_key_required"


def test_reusing_an_electrolysis_idempotency_key_with_a_different_profile_is_a_conflict() -> None:
    client = TestClient(create_app(electrolysis_service=_service()))
    first = client.post(
        "/electrolysis/import-profiles",
        json=electrolysis_profile().model_dump(mode="json"),
        headers={"Idempotency-Key": "same-key"},
    )
    changed = electrolysis_profile().model_copy(update={"missing_value_tokens": ("", "missing")})

    second = client.post(
        "/electrolysis/import-profiles",
        json=changed.model_dump(mode="json"),
        headers={"Idempotency-Key": "same-key"},
    )

    assert first.status_code == CREATED
    assert second.status_code == CONFLICT
    assert second.json()["detail"]["code"] == "electrolysis_idempotency_key_reused"


def test_unknown_electrolysis_profile_is_not_found() -> None:
    client = TestClient(create_app(electrolysis_service=_service()))

    response = client.get("/electrolysis/import-profiles/electrolysis-profile:missing")

    assert response.status_code == NOT_FOUND
    assert response.json()["detail"]["code"] == "electrolysis_profile_not_found"

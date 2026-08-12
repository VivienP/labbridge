from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from cv_helpers import FixedSourceReader, MemoryCVRecords, cv_profile, cv_source
from labbridge import cli
from labbridge.api.app import create_app
from labbridge.application.cv_ingestion import CVIngestionService

runner = CliRunner()
OK = 200
CREATED = 201
BAD_REQUEST = 400
CONFLICT = 409


def _service() -> CVIngestionService:
    return CVIngestionService(FixedSourceReader(), MemoryCVRecords(), producing_version="0.1.0")


def test_api_and_cli_return_the_same_normalised_observation_and_plot(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    service = _service()
    client = TestClient(create_app(cv_service=service))
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(cv_profile().model_dump_json(indent=2), encoding="utf-8")

    api_profile = client.post(
        "/cv/import-profiles",
        json=cv_profile().model_dump(mode="json"),
        headers={"Idempotency-Key": "profile-1"},
    )
    assert api_profile.status_code == CREATED
    profile_id = api_profile.json()["profile_id"]
    api_normalised = client.post(
        "/cv/normalisations",
        json={
            "source_artifact_id": cv_source().artifact.source_artifact_id,
            "profile_id": profile_id,
        },
        headers={"Idempotency-Key": "normalise-1"},
    )
    assert api_normalised.status_code == CREATED
    observation_id = api_normalised.json()["result"]["observation"]["observation_id"]
    api_plot = client.get(f"/cv/normalised-observations/{observation_id}/plot-series")
    assert api_plot.status_code == OK

    monkeypatch.setattr(cli, "_build_cv_service", lambda: service)
    cli_profile = runner.invoke(cli.app, ["cv", "profile-create", str(profile_path), "--json"])
    assert cli_profile.exit_code == 0, cli_profile.output
    cli_normalised = runner.invoke(
        cli.app,
        [
            "cv",
            "normalise",
            cv_source().artifact.source_artifact_id,
            "--profile-id",
            profile_id,
            "--json",
        ],
    )
    assert cli_normalised.exit_code == 0, cli_normalised.output
    cli_plot = runner.invoke(cli.app, ["cv", "plot", observation_id, "--json"])
    assert cli_plot.exit_code == 0, cli_plot.output

    assert json.loads(cli_normalised.output)["result"] == api_normalised.json()["result"]
    assert json.loads(cli_plot.output) == api_plot.json()
    assert api_plot.json()["environment_id"] == cv_profile().environment_id


def test_source_inspection_reports_headers_without_assigning_roles(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    service = _service()
    client = TestClient(create_app(cv_service=service))

    response = client.post(
        "/cv/source-inspections",
        json={
            "source_artifact_id": cv_source().artifact.source_artifact_id,
            "encoding": "utf-8",
            "delimiter": ",",
            "header_row": 1,
        },
    )

    assert response.status_code == OK
    assert response.json()["headers"] == ["sample_index", "channel_a", "channel_b"]
    assert "role" not in response.text
    monkeypatch.setattr(cli, "_build_cv_service", lambda: service)
    command = runner.invoke(
        cli.app,
        [
            "cv",
            "inspect",
            cv_source().artifact.source_artifact_id,
            "--encoding",
            "utf-8",
            "--delimiter",
            ",",
            "--header-row",
            "1",
            "--json",
        ],
    )
    assert command.exit_code == 0, command.output
    assert json.loads(command.output) == response.json()


def test_mutating_cv_endpoints_require_an_idempotency_key() -> None:
    client = TestClient(create_app(cv_service=_service()))

    profile_response = client.post("/cv/import-profiles", json=cv_profile().model_dump(mode="json"))
    normalise_response = client.post(
        "/cv/normalisations",
        json={
            "source_artifact_id": cv_source().artifact.source_artifact_id,
            "profile_id": "cv-profile:any",
        },
    )

    assert profile_response.status_code == BAD_REQUEST
    assert profile_response.json()["detail"]["code"] == "idempotency_key_required"
    assert normalise_response.status_code == BAD_REQUEST
    assert normalise_response.json()["detail"]["code"] == "idempotency_key_required"


def test_reusing_a_cv_idempotency_key_with_a_different_profile_is_a_conflict() -> None:
    client = TestClient(create_app(cv_service=_service()))
    first = client.post(
        "/cv/import-profiles",
        json=cv_profile().model_dump(mode="json"),
        headers={"Idempotency-Key": "same-key"},
    )
    changed = cv_profile().model_copy(update={"missing_value_tokens": ("", "missing")})

    second = client.post(
        "/cv/import-profiles",
        json=changed.model_dump(mode="json"),
        headers={"Idempotency-Key": "same-key"},
    )

    assert first.status_code == CREATED
    assert second.status_code == CONFLICT
    assert second.json()["detail"]["code"] == "cv_idempotency_key_reused"

"""CLI and HTTP translate source bytes into the same application command."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient
from typer.testing import CliRunner

import labbridge.cli as cli_module
from labbridge.api import create_app
from labbridge.application.source_intake import (
    IntakeResult,
    IntakeSource,
    RetrievedSource,
    SourceArtifactService,
)
from labbridge.cli import app
from labbridge.domain.source_artifacts import SourceArtifact, source_artifact_id

PAYLOAD = b"opaque,synthetic,replay\r\n"
OK = 200
CREATED = 201


class CapturingService:
    def __init__(self) -> None:
        self.commands: list[IntakeSource] = []
        digest = hashlib.sha256(PAYLOAD).hexdigest()
        self.artifact = SourceArtifact(
            source_artifact_id=source_artifact_id(
                sha256=digest, byte_size=len(PAYLOAD), media_type="text/csv"
            ),
            filename="synthetic-replay-cv-opaque.csv",
            media_type="text/csv",
            byte_size=len(PAYLOAD),
            sha256=digest,
            data_origin="synthetic",
            execution_mode="replay",
            state="committed",
            object_uri=f"s3://test/source-artifacts/sha256/{digest}",
            created_at=datetime(2026, 8, 12, tzinfo=UTC),
            committed_at=datetime(2026, 8, 12, tzinfo=UTC),
        )

    def intake(self, command: IntakeSource) -> IntakeResult:
        self.commands.append(command)
        return IntakeResult(artifact=self.artifact, replayed=False)

    def lookup(self, source_artifact_id: str) -> SourceArtifact:
        assert source_artifact_id == self.artifact.source_artifact_id
        return self.artifact

    def retrieve(self, source_artifact_id: str) -> RetrievedSource:
        assert source_artifact_id == self.artifact.source_artifact_id
        return RetrievedSource(artifact=self.artifact, data=PAYLOAD)

    def verify(self, source_artifact_id: str) -> SourceArtifact:
        return self.retrieve(source_artifact_id).artifact


def test_cli_and_http_call_the_same_source_intake_use_case(
    tmp_path: Path, monkeypatch: object
) -> None:
    service = CapturingService()
    source = tmp_path / "synthetic-replay-cv-opaque.csv"
    source.write_bytes(PAYLOAD)
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module, "_build_source_service", lambda: cast(SourceArtifactService, service)
    )

    cli_result = CliRunner().invoke(
        app,
        [
            "source",
            "intake",
            str(source),
            "--intake-id",
            "shared-intake",
            "--media-type",
            "text/csv",
            "--data-origin",
            "synthetic",
            "--execution-mode",
            "replay",
        ],
    )
    client = TestClient(create_app(source_service=cast(SourceArtifactService, service)))
    http_result = client.post(
        "/source-artifacts",
        params={
            "filename": source.name,
            "data_origin": "synthetic",
            "execution_mode": "replay",
        },
        headers={"Idempotency-Key": "shared-intake", "Content-Type": "text/csv"},
        content=PAYLOAD,
    )

    assert cli_result.exit_code == 0
    assert http_result.status_code == CREATED
    assert service.commands == [service.commands[0], service.commands[0]]


def test_http_metadata_and_content_are_read_through_the_use_case() -> None:
    service = CapturingService()
    client = TestClient(create_app(source_service=cast(SourceArtifactService, service)))

    metadata = client.get(f"/source-artifacts/{service.artifact.source_artifact_id}")
    content = client.get(f"/source-artifacts/{service.artifact.source_artifact_id}/content")

    assert metadata.status_code == OK
    assert metadata.json()["sha256"] == service.artifact.sha256
    assert metadata.json()["data_origin"] == "synthetic"
    assert metadata.json()["execution_mode"] == "replay"
    assert content.status_code == OK
    assert content.content == PAYLOAD

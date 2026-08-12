"""The Phase 1 artifact demonstrates capture while assigning no CSV semantics."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from labbridge.application.source_intake import RetrievedSource
from labbridge.domain.source_artifacts import SourceArtifact, source_artifact_id
from labbridge.evidence.manifest import verify_manifest
from labbridge.evidence.source_capture import build_source_capture

PAYLOAD = b"x,y\r\n0.0,1.0\r\n"


def _retrieved() -> RetrievedSource:
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    artifact = SourceArtifact(
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
        object_uri=f"s3://labbridge/source-artifacts/sha256/{digest}",
        created_at=datetime(2026, 8, 12, tzinfo=UTC),
        committed_at=datetime(2026, 8, 12, tzinfo=UTC),
    )
    return RetrievedSource(artifact=artifact, data=PAYLOAD)


def test_source_capture_is_closed_verified_and_visibly_synthetic(tmp_path: Path) -> None:
    build_source_capture(_retrieved(), tmp_path, producing_version="0.1.0")

    manifest = verify_manifest(tmp_path)
    record = json.loads((tmp_path / "source-artifact.json").read_text(encoding="utf-8"))
    description = (tmp_path / "LIMITATIONS.md").read_text(encoding="utf-8")

    assert manifest["artifact_kind"] == "source_capture"
    assert manifest["data_origin"] == "synthetic"
    assert manifest["execution_mode"] == "replay"
    assert record["data_origin"] == "synthetic"
    assert record["execution_mode"] == "replay"
    assert "synthetic + replay" in description
    assert "does not interpret" in description
    assert (tmp_path / "synthetic-replay-cv-opaque.csv").read_bytes() == PAYLOAD


def test_rebuilding_source_capture_produces_identical_members(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    build_source_capture(_retrieved(), first, producing_version="0.1.0")
    build_source_capture(_retrieved(), second, producing_version="0.1.0")

    first_members = {path.name: path.read_bytes() for path in first.iterdir()}
    second_members = {path.name: path.read_bytes() for path in second.iterdir()}
    assert first_members == second_members

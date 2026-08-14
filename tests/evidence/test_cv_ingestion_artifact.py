from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from scripts.reproduce_cv_ingestion import reproduce

from labbridge.application.source_intake import RetrievedSource
from labbridge.domain.cv import CVImportProfile
from labbridge.domain.source_artifacts import SourceArtifact, source_artifact_id
from labbridge.evidence.cv_ingestion import build_cv_ingestion_artifact
from labbridge.evidence.manifest import verify_manifest

ROOT = Path(__file__).resolve().parents[2]
COMMITTED = ROOT / "artifacts/cv-ingestion"
SOURCE = ROOT / "fixtures/source/synthetic-replay-cv-opaque.csv"
PROFILE = ROOT / "fixtures/import-profiles/synthetic-replay-cv-v1.json"


def _source() -> RetrievedSource:
    data = SOURCE.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    artifact = SourceArtifact(
        source_artifact_id=source_artifact_id(
            sha256=digest, byte_size=len(data), media_type="text/csv"
        ),
        filename=SOURCE.name,
        media_type="text/csv",
        byte_size=len(data),
        sha256=digest,
        data_origin="synthetic",
        execution_mode="replay",
        state="committed",
        object_uri=f"s3://labbridge/source-artifacts/sha256/{digest}",
        created_at=datetime(2026, 8, 12, tzinfo=UTC),
        committed_at=datetime(2026, 8, 12, tzinfo=UTC),
    )
    return RetrievedSource(artifact=artifact, data=data)


def test_phase_2_artifact_is_closed_and_preserves_the_phase_1_source(tmp_path: Path) -> None:
    profile = CVImportProfile.model_validate_json(PROFILE.read_text(encoding="utf-8"))

    build_cv_ingestion_artifact(_source(), profile, tmp_path, producing_version="0.1.0")

    manifest = verify_manifest(tmp_path)
    phase_1 = json.loads(
        (ROOT / "artifacts/source-capture/source-artifact.json").read_text(encoding="utf-8")
    )
    observation = json.loads((tmp_path / "normalised-observation.json").read_text(encoding="utf-8"))
    graph = json.loads((tmp_path / "transformation-graph.json").read_text(encoding="utf-8"))
    assert manifest["artifact_kind"] == "generic_cv_ingestion"
    assert manifest["source_artifact_id"] == phase_1["source_artifact_id"]
    assert hashlib.sha256((tmp_path / SOURCE.name).read_bytes()).hexdigest() == phase_1["sha256"]
    assert observation["source_artifact_id"] == phase_1["source_artifact_id"]
    assert observation["environment_id"] == profile.environment_id
    assert manifest["environment_id"] == profile.environment_id
    assert graph["source_artifact_id"] == phase_1["source_artifact_id"]
    assert graph["observation_id"] == observation["observation_id"]


def test_phase_2_artifact_rebuild_is_byte_identical(tmp_path: Path) -> None:
    profile = CVImportProfile.model_validate_json(PROFILE.read_text(encoding="utf-8"))
    first = tmp_path / "first"
    second = tmp_path / "second"

    build_cv_ingestion_artifact(_source(), profile, first, producing_version="0.1.0")
    build_cv_ingestion_artifact(_source(), profile, second, producing_version="0.1.0")

    assert {item.name: item.read_bytes() for item in first.iterdir()} == {
        item.name: item.read_bytes() for item in second.iterdir()
    }


def test_committed_artifact_bytes_verify_against_their_committed_manifest() -> None:
    """The bytes in the repository, not a fresh rebuild, must satisfy the committed manifest.

    Both earlier tests build into `tmp_path`, so a checkout that rewrote the committed members —
    line-ending conversion being the obvious way — left the whole offline suite green while
    `labbridge validate-artifacts --bundle artifacts/cv-ingestion` failed.
    """
    manifest = verify_manifest(COMMITTED)

    assert manifest["artifact_kind"] == "generic_cv_ingestion"
    assert manifest["data_origin"] == "synthetic"
    for entry in manifest["files"]:
        assert isinstance(entry, dict)
        member = COMMITTED / str(entry["name"])
        payload = member.read_bytes()
        assert hashlib.sha256(payload).hexdigest() == entry["sha256"], member.name
        assert len(payload) == entry["byte_size"], member.name


def test_committed_source_member_still_matches_the_phase_1_fixture_bytes() -> None:
    """Content identity has to survive checkout, or the source link is broken where it is read."""
    committed_source = (COMMITTED / SOURCE.name).read_bytes()
    manifest = json.loads((COMMITTED / "manifest.json").read_text(encoding="utf-8"))

    assert committed_source == SOURCE.read_bytes()
    assert hashlib.sha256(committed_source).hexdigest() == manifest["source_sha256"]


def test_committed_artifact_is_byte_identical_to_an_offline_rebuild(tmp_path: Path) -> None:
    reproduce(tmp_path / "rebuilt")

    assert {item.name: item.read_bytes() for item in (tmp_path / "rebuilt").iterdir()} == {
        item.name: item.read_bytes() for item in COMMITTED.iterdir()
    }

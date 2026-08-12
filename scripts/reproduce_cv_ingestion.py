"""Reproduce and verify the committed Phase 2 generic CV ingestion artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from labbridge import __version__
from labbridge.application.source_intake import RetrievedSource
from labbridge.domain.cv import CVImportProfile
from labbridge.domain.source_artifacts import SourceArtifact, source_artifact_id
from labbridge.evidence.cv_ingestion import build_cv_ingestion_artifact
from labbridge.evidence.manifest import verify_manifest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPOSITORY_ROOT / "fixtures/source/synthetic-replay-cv-opaque.csv"
PROFILE = REPOSITORY_ROOT / "fixtures/import-profiles/synthetic-replay-cv-v1.json"
PHASE_1_RECORD = REPOSITORY_ROOT / "artifacts/source-capture/source-artifact.json"


def _retained_source() -> RetrievedSource:
    data = SOURCE.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    recorded = json.loads(PHASE_1_RECORD.read_text(encoding="utf-8"))
    identity = source_artifact_id(sha256=digest, byte_size=len(data), media_type="text/csv")
    if digest != recorded["sha256"] or identity != recorded["source_artifact_id"]:
        raise ValueError("Phase 1 source bytes no longer match the committed source-capture record")
    timestamp = datetime(2026, 8, 12, tzinfo=UTC)
    artifact = SourceArtifact(
        source_artifact_id=identity,
        filename=SOURCE.name,
        media_type="text/csv",
        byte_size=len(data),
        sha256=digest,
        data_origin="synthetic",
        execution_mode="replay",
        state="committed",
        object_uri=recorded["object_uri"],
        created_at=timestamp,
        committed_at=timestamp,
    )
    return RetrievedSource(artifact=artifact, data=data)


def reproduce(output: Path) -> dict[str, object]:
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"refusing to replace non-empty artifact directory: {output}")
    profile = CVImportProfile.model_validate_json(PROFILE.read_text(encoding="utf-8"))
    build_cv_ingestion_artifact(_retained_source(), profile, output, producing_version=__version__)
    return verify_manifest(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/cv-ingestion"))
    args = parser.parse_args()
    manifest = reproduce(args.output)
    print(f"verified {manifest['artifact_kind']} {manifest['observation_id']} at {args.output}")


if __name__ == "__main__":
    main()

"""Reproducible evidence for the minimum opaque source-capture seam."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from labbridge.application.source_intake import RetrievedSource

from .manifest import build_manifest, canonical_json

SOURCE_CAPTURE_SCHEMA_VERSION: Final = "1"
REPRODUCTION_COMMAND: Final = (
    "python scripts/reproduce_source_capture.py --output build/source-capture\n"
)
LIMITATIONS: Final = """# Source-capture evidence limitations

This artifact contains opaque bytes generated independently for a `synthetic + replay` source.
It demonstrates retention and checksum verification of those exact bytes. Phase 1 does not interpret
CSV columns, assign units, infer a reference scale, or validate a technique. It does not normalise
values or produce an Experiment Passport.
"""


def build_source_capture(
    retrieved: RetrievedSource,
    destination: Path,
    *,
    producing_version: str,
) -> dict[str, object]:
    """Write a flat closed artifact from bytes already verified by the application service."""
    artifact = retrieved.artifact
    if artifact.data_origin != "synthetic" or artifact.execution_mode != "replay":
        raise ValueError("the Phase 1 source-capture artifact must be synthetic + replay")
    destination.mkdir(parents=True, exist_ok=True)
    (destination / artifact.filename).write_bytes(retrieved.data)
    record = {
        "schema_version": SOURCE_CAPTURE_SCHEMA_VERSION,
        "source_artifact_id": artifact.source_artifact_id,
        "filename": artifact.filename,
        "media_type": artifact.media_type,
        "byte_size": artifact.byte_size,
        "sha256": artifact.sha256,
        "data_origin": artifact.data_origin,
        "execution_mode": artifact.execution_mode,
        "state": artifact.state,
        "object_uri": artifact.object_uri,
    }
    (destination / "source-artifact.json").write_bytes(canonical_json(record))
    verification = {
        "schema_version": SOURCE_CAPTURE_SCHEMA_VERSION,
        "source_artifact_id": artifact.source_artifact_id,
        "verified": True,
        "verification_method": "object-store read-back sha256 and byte-size comparison",
        "byte_size": artifact.byte_size,
        "sha256": artifact.sha256,
        "data_origin": artifact.data_origin,
        "execution_mode": artifact.execution_mode,
    }
    (destination / "verification.json").write_bytes(canonical_json(verification))
    (destination / "LIMITATIONS.md").write_text(LIMITATIONS, encoding="utf-8", newline="\n")
    (destination / "REPRODUCE.txt").write_text(REPRODUCTION_COMMAND, encoding="utf-8", newline="\n")
    return build_manifest(
        destination,
        metadata={
            "artifact_kind": "source_capture",
            "schema_version": SOURCE_CAPTURE_SCHEMA_VERSION,
            "producing_versions": {"labbridge": producing_version},
            "source_artifact_id": artifact.source_artifact_id,
            "source_filename": artifact.filename,
            "data_origin": artifact.data_origin,
            "execution_mode": artifact.execution_mode,
            "description": "Opaque synthetic + replay source capture; no CSV interpretation.",
        },
    )


__all__ = ["build_source_capture"]

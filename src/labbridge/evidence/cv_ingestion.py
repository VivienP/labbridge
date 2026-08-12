"""Closed reproducible evidence for explicit generic CV CSV ingestion."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from labbridge.application.cv_ingestion import normalise_cv
from labbridge.application.source_intake import RetrievedSource
from labbridge.domain.cv import CVImportProfile, import_profile_id

from .manifest import build_manifest, canonical_json

CV_INGESTION_ARTIFACT_SCHEMA_VERSION: Final = "1"
REPRODUCTION_COMMAND: Final = (
    "python scripts/reproduce_cv_ingestion.py --output build/cv-ingestion\n"
)
LIMITATIONS: Final = """# Generic CV ingestion evidence limitations

This artifact demonstrates deterministic generic CSV parsing and CV normalisation for one explicit
`synthetic + replay` import profile. The profile, not the filename or headers, assigns column roles,
units, parser settings, and metadata states. Unknown or unavailable context remains explicit.

It does not demonstrate automatic column or unit detection, electrochemical interpretation,
scientific quality scoring, an Experiment Passport or Package, Gamry ingestion, corrections,
background subtraction, iR correction, campaign orchestration, or a user interface.
"""


def build_cv_ingestion_artifact(
    source: RetrievedSource,
    profile: CVImportProfile,
    destination: Path,
    *,
    producing_version: str,
) -> dict[str, object]:
    """Write a flat, closed Phase 2 artifact from exact source bytes and one explicit profile."""
    artifact = source.artifact
    if artifact.data_origin != "synthetic" or artifact.execution_mode != "replay":
        raise ValueError("the Phase 2 acceptance artifact must be synthetic + replay")
    result = normalise_cv(source, profile, producing_version=producing_version)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / artifact.filename).write_bytes(source.data)
    (destination / "import-profile.json").write_bytes(
        canonical_json(profile.model_dump(mode="json"))
    )
    (destination / "normalised-observation.json").write_bytes(
        canonical_json(result.observation.model_dump(mode="json"))
    )
    (destination / "transformation-graph.json").write_bytes(
        canonical_json(result.graph.model_dump(mode="json"))
    )
    (destination / "structural-findings.json").write_bytes(
        canonical_json([finding.model_dump(mode="json") for finding in result.findings])
    )
    verification = {
        "schema_version": CV_INGESTION_ARTIFACT_SCHEMA_VERSION,
        "verified": True,
        "source_artifact_id": artifact.source_artifact_id,
        "source_sha256": artifact.sha256,
        "import_profile_id": import_profile_id(profile),
        "observation_id": result.observation.observation_id,
        "environment_id": result.observation.environment_id,
        "lineage_closed": result.graph.is_closed,
        "structural_findings": [
            {"code": finding.code, "status": finding.status} for finding in result.findings
        ],
    }
    (destination / "verification.json").write_bytes(canonical_json(verification))
    (destination / "LIMITATIONS.md").write_text(LIMITATIONS, encoding="utf-8", newline="\n")
    (destination / "REPRODUCE.txt").write_text(REPRODUCTION_COMMAND, encoding="utf-8", newline="\n")
    return build_manifest(
        destination,
        metadata={
            "artifact_kind": "generic_cv_ingestion",
            "schema_version": CV_INGESTION_ARTIFACT_SCHEMA_VERSION,
            "producing_versions": {
                "labbridge": producing_version,
                "cv_csv_parser": "1",
                "normalised_cv_observation": "1",
            },
            "source_artifact_id": artifact.source_artifact_id,
            "source_filename": artifact.filename,
            "source_sha256": artifact.sha256,
            "import_profile_id": import_profile_id(profile),
            "observation_id": result.observation.observation_id,
            "data_origin": artifact.data_origin,
            "execution_mode": artifact.execution_mode,
            "environment_id": result.observation.environment_id,
            "description": "Explicit generic CV CSV normalisation with closed source lineage.",
        },
    )


__all__ = ["build_cv_ingestion_artifact"]

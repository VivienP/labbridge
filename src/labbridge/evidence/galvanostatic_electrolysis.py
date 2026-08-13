"""Reproducible evidence for bounded galvanostatic-electrolysis packaging."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from labbridge.application.electrolysis_ingestion import normalise_electrolysis
from labbridge.application.experiments import experiment_from_electrolysis_normalisation
from labbridge.application.source_intake import RetrievedSource
from labbridge.domain.electrolysis import (
    ElectrolysisImportProfile,
    electrolysis_import_profile_id,
)
from labbridge.domain.experiments import validate_experiment
from labbridge.domain.source_artifacts import SourceArtifact, source_artifact_id

from .experiment_package import PackageInputs, build_experiment_package, verify_experiment_package
from .manifest import build_manifest, canonical_json, verify_manifest
from .passport import build_passport, render_passport_html, render_passport_json

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[3]
SOURCE: Final = REPOSITORY_ROOT / "fixtures/source/synthetic-galvanostatic-electrolysis.csv"
PROFILE: Final = (
    REPOSITORY_ROOT / "fixtures/import-profiles/synthetic-galvanostatic-electrolysis-v1.json"
)
FIXTURE_PROVENANCE: Final = (
    REPOSITORY_ROOT / "fixtures/source/synthetic-galvanostatic-electrolysis.provenance.json"
)
RELEASED_AT: Final = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
REPRODUCTION_COMMAND: Final = (
    "python scripts/reproduce_galvanostatic_electrolysis.py "
    "--output build/galvanostatic-electrolysis\n"
    "labbridge package verify build/galvanostatic-electrolysis/experiment-package.zip --json\n"
    "labbridge validate-artifacts build/galvanostatic-electrolysis\n"
)
LIMITATIONS: Final = """# Galvanostatic electrolysis package limitations

This candidate artifact exercises one project-owned `synthetic + replay` fixture. It records only
time, total current, and potential series with explicit mappings and units. The current sign
convention, cell geometry, reference scale, and potential correction state remain `unknown`.

Chemical analysis is `unavailable`. The Passport and Package do not report conversion,
selectivity, yield, product assignment, or Faradaic efficiency. Auxiliary analytical results are
accepted only as declarations that name exact electrical and analytical sources, sample and
collection point, declared methods and versions, source locations, quantity kinds, values, and
units; this fixture intentionally includes none. The capability status is `implemented`; this
uncommitted candidate is not evidence of clean-checkout demonstration or live instrument operation.
"""


def _source() -> RetrievedSource:
    data = SOURCE.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    identity = source_artifact_id(sha256=sha256, byte_size=len(data), media_type="text/csv")
    return RetrievedSource(
        artifact=SourceArtifact(
            source_artifact_id=identity,
            filename=SOURCE.name,
            media_type="text/csv",
            byte_size=len(data),
            sha256=sha256,
            data_origin="synthetic",
            execution_mode="replay",
            state="committed",
            object_uri=f"s3://labbridge/source-artifacts/sha256/{sha256}",
            created_at=RELEASED_AT,
            committed_at=RELEASED_AT,
        ),
        data=data,
    )


def reproduce_galvanostatic_electrolysis_artifact(
    destination: Path,
    *,
    producing_version: str,
) -> dict[str, object]:
    """Build and verify the deterministic galvanostatic-electrolysis candidate artifact."""
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"refusing to replace non-empty artifact directory: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    source = _source()
    profile = ElectrolysisImportProfile.model_validate_json(PROFILE.read_text(encoding="utf-8"))
    fixture_provenance = json.loads(FIXTURE_PROVENANCE.read_text(encoding="utf-8"))
    if not isinstance(fixture_provenance, dict):
        raise ValueError("fixture provenance must contain a JSON object")
    result = normalise_electrolysis(source, profile, producing_version=producing_version)
    experiment = experiment_from_electrolysis_normalisation(result)
    validation = validate_experiment(experiment, validation_version="2")
    passport = build_passport(
        experiment,
        validation,
        released_at=RELEASED_AT,
        release=True,
        supersedes_passport_id=None,
    )
    producing_versions = {
        "experiment_package": "3",
        "labbridge": producing_version,
    }
    package = build_experiment_package(
        PackageInputs(
            source_filename=source.artifact.filename,
            source_bytes=source.data,
            source_artifact={
                "schema_version": "1",
                **source.artifact.model_dump(mode="json"),
            },
            import_profile={
                "profile_id": electrolysis_import_profile_id(profile),
                **profile.model_dump(mode="json"),
            },
            normalised_observation=result.observation.model_dump(mode="json"),
            transformation_graph=result.graph.model_dump(mode="json"),
            passport=passport,
        ),
        producing_versions=producing_versions,
    )
    package_verification = verify_experiment_package(package.archive_bytes)

    (destination / source.artifact.filename).write_bytes(source.data)
    (destination / "source-artifact.json").write_bytes(
        canonical_json({"schema_version": "1", **source.artifact.model_dump(mode="json")})
    )
    (destination / "fixture-provenance.json").write_bytes(canonical_json(fixture_provenance))
    (destination / "import-profile.json").write_bytes(
        canonical_json(
            {
                "profile_id": electrolysis_import_profile_id(profile),
                **profile.model_dump(mode="json"),
            }
        )
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
    (destination / "passport.json").write_bytes(render_passport_json(passport))
    (destination / "passport.html").write_bytes(render_passport_html(passport))
    (destination / "validation.json").write_bytes(
        canonical_json(validation.model_dump(mode="json"))
    )
    (destination / "experiment-package.zip").write_bytes(package.archive_bytes)
    (destination / "package-verification.json").write_bytes(
        canonical_json(package_verification.model_dump(mode="json"))
    )
    verification = {
        "schema_version": "1",
        "verified": True,
        "capability_status": "implemented",
        "source_artifact_id": source.artifact.source_artifact_id,
        "source_sha256": source.artifact.sha256,
        "import_profile_id": electrolysis_import_profile_id(profile),
        "observation_id": result.observation.observation_id,
        "passport_id": passport.passport_id,
        "package_id": package.package_id,
        "electrical_series_complete": True,
        "chemical_analysis": "unavailable",
        "lineage_closed": result.graph.is_closed and package_verification.lineage_closed,
        "package_verified": package_verification.verified,
    }
    (destination / "verification.json").write_bytes(canonical_json(verification))
    (destination / "LIMITATIONS.md").write_text(LIMITATIONS, encoding="utf-8", newline="\n")
    (destination / "REPRODUCE.txt").write_text(REPRODUCTION_COMMAND, encoding="utf-8", newline="\n")
    build_manifest(
        destination,
        metadata={
            "artifact_kind": "galvanostatic_electrolysis_package",
            "schema_version": "1",
            "capability_status": "implemented",
            "producing_versions": producing_versions,
            "data_origin": source.artifact.data_origin,
            "execution_mode": source.artifact.execution_mode,
            "environment_id": result.observation.environment_id,
            "source_artifact_id": source.artifact.source_artifact_id,
            "source_filename": source.artifact.filename,
            "source_sha256": source.artifact.sha256,
            "import_profile_id": electrolysis_import_profile_id(profile),
            "observation_id": result.observation.observation_id,
            "passport_id": passport.passport_id,
            "package_id": package.package_id,
            "description": (
                "Galvanostatic electrical time-series normalisation, technique-aware Passport, "
                "and standalone-verified Package with chemical claims explicitly unavailable."
            ),
        },
    )
    return verify_manifest(destination)


__all__ = ["reproduce_galvanostatic_electrolysis_artifact"]

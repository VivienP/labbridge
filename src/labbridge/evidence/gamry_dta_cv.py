"""Reproducible evidence for bounded Gamry DTA CV ingestion."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from labbridge.application.cv_ingestion import normalise_cv
from labbridge.application.experiments import experiment_from_normalisation
from labbridge.application.source_intake import RetrievedSource
from labbridge.domain.cv import CVImportProfile, import_profile_id
from labbridge.domain.experiments import validate_experiment
from labbridge.domain.source_artifacts import SourceArtifact, source_artifact_id
from labbridge.infrastructure.gamry_dta import PARSER_VERSION

from .experiment_package import (
    PackageInputs,
    build_experiment_package,
    verify_experiment_package,
)
from .manifest import build_manifest, canonical_json, verify_manifest
from .passport import build_passport, render_passport_html, render_passport_json

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[3]
SOURCE: Final = REPOSITORY_ROOT / "fixtures/source/synthetic-gamry-cv.dta"
PROFILE: Final = REPOSITORY_ROOT / "fixtures/import-profiles/synthetic-gamry-cv-v1.json"
FIXTURE_PROVENANCE: Final = REPOSITORY_ROOT / "fixtures/source/synthetic-gamry-cv.provenance.json"
RELEASED_AT: Final = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)
REPRODUCTION_COMMAND: Final = (
    "python scripts/reproduce_gamry_dta_cv.py --output build/gamry-dta-cv\n"
    "labbridge package verify build/gamry-dta-cv/experiment-package.zip --json\n"
    "labbridge validate-artifacts build/gamry-dta-cv\n"
)
SUPPORT: Final = """# Supported Gamry DTA CV variant

The bounded parser accepts UTF-8 or declared UTF-8 BOM text containing `EXPLAIN`, `TAG\tCV`, one
`FRAMEWORKVERSION\tQUANT\t7.07` object, and exactly one `CURVE\tTABLE` object. The declared table
must contain these columns and units in order:

| Column | Source unit | Explicit role |
| --- | --- | --- |
| `Pt` | `#` | ignored |
| `T` | `s` | time in `s` |
| `Vf` | `V vs. Ref.` | potential in `V` |
| `Im` | `A` | current in `A` |
| `Vu`, `Sig`, `Ach`, `IERange`, `Over`, `Temp` | declared by DTA | ignored |
| `Cycle` | `#` | cycle index in `1` |

Decimal point and decimal comma are supported only when the import profile declares the matching
convention. The parser verifies the declared row count and records exact header, unit, and data-line
locations for every accepted scientific field. `V vs. Ref.` is retained as the source unit; the
reference scale remains `unknown` and no electrochemical convention is inferred.

## Parser decision

`echemdb-converters` 0.4.1 was evaluated at source-code level. Its Gamry loader locates a first
`CURVE` table and delegates table parsing, but it does not provide the bounded variant validation,
mixed-block rejection, durable diagnostics, or field-level line provenance required here. LabBridge
therefore uses a small in-repository parser and adds no runtime dependency. References:

- https://github.com/echemdb/echemdb-converters/blob/main/echemdbconverters/gamryloader.py
- https://help.gamry.com/Framework/general-information_datafileformat.html
"""
LIMITATIONS: Final = """# Gamry DTA CV ingestion limitations

This candidate artifact exercises one project-owned `synthetic + replay` fixture. It supports only
the variant listed in `SUPPORT.md`. Other Framework versions, techniques, table schemas, multiple or
mixed table objects, missing or extra rows, undeclared encodings or decimal conventions, and
ambiguous unit mappings fail closed with a retained parser record.

The parser does not infer a reference electrode or potential scale, convert a potential to RHE or
SHE, infer working-electrode area, normalise current to current density, interpret temperature or
instrument-range fields, repair truncated files, select among multiple curves, or claim scientific
validity. Unknown metadata remains unknown. The capability status is `implemented`; this uncommitted
candidate is not evidence of clean-checkout demonstration or production deployment.
"""


def _source() -> RetrievedSource:
    data = SOURCE.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    identity = source_artifact_id(
        sha256=sha256,
        byte_size=len(data),
        media_type="application/vnd.gamry.dta",
    )
    artifact = SourceArtifact(
        source_artifact_id=identity,
        filename=SOURCE.name,
        media_type="application/vnd.gamry.dta",
        byte_size=len(data),
        sha256=sha256,
        data_origin="synthetic",
        execution_mode="replay",
        state="committed",
        object_uri=f"s3://labbridge/source-artifacts/sha256/{sha256}",
        created_at=RELEASED_AT,
        committed_at=RELEASED_AT,
    )
    return RetrievedSource(artifact=artifact, data=data)


def reproduce_gamry_dta_cv_artifact(
    destination: Path,
    *,
    producing_version: str,
) -> dict[str, object]:
    """Build and verify the deterministic Phase 4 candidate artifact."""
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"refusing to replace non-empty artifact directory: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    source = _source()
    profile = CVImportProfile.model_validate_json(PROFILE.read_text(encoding="utf-8"))
    fixture_provenance = json.loads(FIXTURE_PROVENANCE.read_text(encoding="utf-8"))
    if not isinstance(fixture_provenance, dict):
        raise ValueError("fixture provenance must contain a JSON object")
    result = normalise_cv(
        source,
        profile,
        producing_version=producing_version,
        source_format="gamry_dta",
    )
    if result.parser_record is None:
        raise ValueError("Gamry DTA normalisation did not return a parser record")
    experiment = experiment_from_normalisation(result)
    validation = validate_experiment(experiment, validation_version="1")
    passport = build_passport(
        experiment,
        validation,
        released_at=RELEASED_AT,
        release=True,
        supersedes_passport_id=None,
    )
    producing_versions = {
        "experiment_package": "2",
        "gamry_dta_parser": result.parser_record.parser_version,
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
                "profile_id": import_profile_id(profile),
                **profile.model_dump(mode="json"),
            },
            normalised_observation=result.observation.model_dump(mode="json"),
            transformation_graph=result.graph.model_dump(mode="json"),
            parser_record=result.parser_record.model_dump(mode="json"),
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
            {"profile_id": import_profile_id(profile), **profile.model_dump(mode="json")}
        )
    )
    (destination / "parser-record.json").write_bytes(
        canonical_json(result.parser_record.model_dump(mode="json"))
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
        "import_profile_id": import_profile_id(profile),
        "parser_record_id": result.parser_record.parser_record_id,
        "parser_version": PARSER_VERSION,
        "observation_id": result.observation.observation_id,
        "passport_id": passport.passport_id,
        "package_id": package.package_id,
        "lineage_closed": result.graph.is_closed and package_verification.lineage_closed,
        "package_verified": package_verification.verified,
    }
    (destination / "verification.json").write_bytes(canonical_json(verification))
    (destination / "SUPPORT.md").write_text(SUPPORT, encoding="utf-8", newline="\n")
    (destination / "LIMITATIONS.md").write_text(LIMITATIONS, encoding="utf-8", newline="\n")
    (destination / "REPRODUCE.txt").write_text(REPRODUCTION_COMMAND, encoding="utf-8", newline="\n")
    build_manifest(
        destination,
        metadata={
            "artifact_kind": "gamry_dta_cv_ingestion",
            "schema_version": "1",
            "capability_status": "implemented",
            "producing_versions": producing_versions,
            "data_origin": source.artifact.data_origin,
            "execution_mode": source.artifact.execution_mode,
            "environment_id": result.observation.environment_id,
            "source_artifact_id": source.artifact.source_artifact_id,
            "source_filename": source.artifact.filename,
            "source_sha256": source.artifact.sha256,
            "import_profile_id": import_profile_id(profile),
            "parser_record_id": result.parser_record.parser_record_id,
            "observation_id": result.observation.observation_id,
            "passport_id": passport.passport_id,
            "package_id": package.package_id,
            "description": (
                "Bounded Gamry DTA CV parsing with exact source-field locations, shared CV "
                "normalisation, Passport provenance, and an independently verified Package."
            ),
        },
    )
    return verify_manifest(destination)


__all__ = ["reproduce_gamry_dta_cv_artifact"]

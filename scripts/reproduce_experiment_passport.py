"""Reproduce the versioned Experiment Passports and verified Packages demonstration."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from labbridge import __version__
from labbridge.application.cv_ingestion import normalise_cv
from labbridge.application.experiments import experiment_from_normalisation
from labbridge.application.source_intake import RetrievedSource
from labbridge.cli import app
from labbridge.domain.cv import CVImportProfile, import_profile_id
from labbridge.domain.experiments import AssertionValue, add_user_assertion, validate_experiment
from labbridge.domain.source_artifacts import SourceArtifact
from labbridge.evidence.experiment_package import PackageInputs, build_experiment_package
from labbridge.evidence.manifest import build_manifest, canonical_json, digest, verify_manifest
from labbridge.evidence.passport import (
    build_passport,
    render_passport_html,
    render_passport_json,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPOSITORY_ROOT / "fixtures/source/synthetic-replay-cv-opaque.csv"
PROFILE = REPOSITORY_ROOT / "fixtures/import-profiles/synthetic-replay-cv-v1.json"
PHASE_1_RECORD = REPOSITORY_ROOT / "artifacts/source-capture/source-artifact.json"
INITIAL_RELEASED_AT = datetime(2026, 8, 12, 20, 0, tzinfo=UTC)
SUPERSEDING_RELEASED_AT = datetime(2026, 8, 12, 20, 5, tzinfo=UTC)
REPRODUCTION_COMMAND = (
    "python scripts/reproduce_experiment_passport.py --output build/experiment-passport\n"
    "labbridge package verify build/experiment-passport/initial-package.zip --json\n"
    "labbridge package verify build/experiment-passport/superseding-package.zip --json\n"
)
LIMITATIONS = """# Experiment Passport demonstration limitations

This artifact demonstrates versioned metadata assertions, deterministic validation, JSON and HTML
Passport parity, append-only user supplementation, immutable superseding releases, closed Phase 1-2
lineage, and independent package checksum verification for one `synthetic + replay` CV fixture.

The user-supplied `RHE` assertion is a demonstration declaration. LabBridge does not infer or verify
that scientific context from the source bytes. The artifact does not claim scientific validity,
data quality, experimental reproducibility, journal readiness, or fitness for a specific use.
"""


def _source() -> RetrievedSource:
    record = json.loads(PHASE_1_RECORD.read_text(encoding="utf-8"))
    record.pop("schema_version")
    timestamp = datetime(2026, 8, 12, tzinfo=UTC)
    artifact = SourceArtifact(**record, created_at=timestamp, committed_at=timestamp)
    return RetrievedSource(artifact=artifact, data=SOURCE.read_bytes())


def _inputs(source: RetrievedSource, profile: CVImportProfile, result, passport) -> PackageInputs:
    return PackageInputs(
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
        passport=passport,
    )


def _cli_verification(package_path: Path) -> dict[str, object]:
    invoked = CliRunner().invoke(app, ["package", "verify", str(package_path), "--json"])
    if invoked.exit_code != 0:
        raise RuntimeError(f"CLI package verification failed: {invoked.stdout}")
    parsed = json.loads(invoked.stdout)
    if not isinstance(parsed, dict):
        raise RuntimeError("CLI package verification did not return an object")
    return parsed


def reproduce(output: Path) -> dict[str, object]:
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"refusing to replace non-empty artifact directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    source = _source()
    profile = CVImportProfile.model_validate_json(PROFILE.read_text(encoding="utf-8"))
    result = normalise_cv(source, profile, producing_version=__version__)
    initial_experiment = experiment_from_normalisation(result)
    initial_validation = validate_experiment(initial_experiment, validation_version="1")
    initial_passport = build_passport(
        initial_experiment,
        initial_validation,
        released_at=INITIAL_RELEASED_AT,
        release=True,
    )
    producing_versions = {"labbridge": __version__, "experiment_package": "1"}
    initial_package = build_experiment_package(
        _inputs(source, profile, result, initial_passport),
        producing_versions=producing_versions,
    )
    profile_assertion = next(
        assertion
        for assertion in initial_experiment.assertions
        if assertion.field_name == "reference_scale" and assertion.origin == "user_supplied"
    )
    source_assertion = next(
        assertion
        for assertion in initial_experiment.assertions
        if assertion.field_name == "source_artifact" and assertion.origin == "source_file"
    )
    superseding_experiment = add_user_assertion(
        initial_experiment,
        expected_version=1,
        field_name="reference_scale",
        requirement_class="conditional",
        transformation="none",
        value=AssertionValue(state="known", value="RHE"),
        evidence_note="Operator declaration retained as user-supplied demonstration evidence.",
        supplements_assertion_id=profile_assertion.assertion_id,
    )
    superseding_validation = validate_experiment(superseding_experiment, validation_version="1")
    superseding_passport = build_passport(
        superseding_experiment,
        superseding_validation,
        released_at=SUPERSEDING_RELEASED_AT,
        release=True,
        supersedes_passport_id=initial_passport.passport_id,
    )
    superseding_package = build_experiment_package(
        _inputs(source, profile, result, superseding_passport),
        producing_versions=producing_versions,
        supersedes_package_id=initial_package.package_id,
    )
    rebuilt_initial = build_experiment_package(
        _inputs(source, profile, result, initial_passport),
        producing_versions=producing_versions,
    )
    source_after = next(
        assertion
        for assertion in superseding_experiment.assertions
        if assertion.assertion_id == source_assertion.assertion_id
    )

    (output / "initial-passport.json").write_bytes(render_passport_json(initial_passport))
    (output / "initial-passport.html").write_bytes(render_passport_html(initial_passport))
    (output / "initial-package.zip").write_bytes(initial_package.archive_bytes)
    (output / "superseding-passport.json").write_bytes(render_passport_json(superseding_passport))
    (output / "superseding-passport.html").write_bytes(render_passport_html(superseding_passport))
    (output / "superseding-package.zip").write_bytes(superseding_package.archive_bytes)
    (output / "initial-findings.json").write_bytes(
        canonical_json(initial_validation.model_dump(mode="json"))
    )
    (output / "superseding-findings.json").write_bytes(
        canonical_json(superseding_validation.model_dump(mode="json"))
    )
    immutability = {
        "schema_version": "1",
        "source_assertion_id": source_assertion.assertion_id,
        "source_assertion_before": source_assertion.model_dump(mode="json"),
        "source_assertion_after": source_after.model_dump(mode="json"),
        "source_assertion_unchanged": source_after == source_assertion,
        "initial_package_sha256_before_supplement": digest(initial_package.archive_bytes),
        "initial_package_sha256_after_supplement": digest(rebuilt_initial.archive_bytes),
        "initial_package_unchanged_after_supplement": (
            initial_package.archive_bytes == rebuilt_initial.archive_bytes
        ),
        "user_assertion_id": superseding_experiment.assertions[-1].assertion_id,
    }
    (output / "assertion-immutability.json").write_bytes(canonical_json(immutability))
    cli_output = {
        "schema_version": "1",
        "initial": _cli_verification(output / "initial-package.zip"),
        "superseding": _cli_verification(output / "superseding-package.zip"),
    }
    (output / "cli-verification.json").write_bytes(canonical_json(cli_output))
    (output / "LIMITATIONS.md").write_text(LIMITATIONS, encoding="utf-8", newline="\n")
    (output / "REPRODUCE.txt").write_text(REPRODUCTION_COMMAND, encoding="utf-8", newline="\n")
    build_manifest(
        output,
        metadata={
            "artifact_kind": "experiment_passport_and_verified_package",
            "schema_version": "1",
            "capability_status": "implemented",
            "producing_versions": producing_versions,
            "data_origin": initial_passport.data_origin,
            "execution_mode": initial_passport.execution_mode,
            "source_artifact_id": initial_passport.source_artifact_id,
            "observation_id": initial_passport.observation_id,
            "initial_passport_id": initial_passport.passport_id,
            "initial_package_id": initial_package.package_id,
            "superseding_passport_id": superseding_passport.passport_id,
            "superseding_package_id": superseding_package.package_id,
            "description": (
                "Versioned Passports and independently verified Packages with append-only "
                "user supplementation and closed retained-source lineage."
            ),
        },
    )
    return verify_manifest(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/experiment-passport"))
    args = parser.parse_args()
    manifest = reproduce(args.output)
    print(
        "verified "
        f"{manifest['artifact_kind']} {manifest['superseding_package_id']} at {args.output}"
    )


if __name__ == "__main__":
    main()

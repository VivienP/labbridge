"""Reproducible Phase 6 EchemDB-aligned CV exchange evidence."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

from labbridge.application.cv_ingestion import normalise_cv
from labbridge.application.experiments import experiment_from_normalisation
from labbridge.application.source_intake import RetrievedSource
from labbridge.domain.cv import CVImportProfile
from labbridge.domain.experiments import (
    AssertionValue,
    Experiment,
    RequirementClass,
    add_user_assertion,
)
from labbridge.domain.source_artifacts import SourceArtifact, source_artifact_id

from .echemdb_exchange import (
    ADAPTER_VERSION,
    DATA_PACKAGE_PROFILE_VERSION,
    ECHEMDB_SCHEMA_COMMIT,
    ECHEMDB_SCHEMA_VERSION,
    FRICTIONLESS_VERSION,
    JSONSCHEMA_VERSION,
    REFERENCING_VERSION,
    MappingReport,
    build_cv_exchange,
    validate_exchange,
)
from .manifest import build_manifest, canonical_json, verify_manifest

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[3]
SOURCE: Final = REPOSITORY_ROOT / "fixtures/source/synthetic-gamry-cv.dta"
PROFILE: Final = REPOSITORY_ROOT / "fixtures/import-profiles/synthetic-gamry-cv-v1.json"
EXCHANGE_PROFILE: Final = (
    REPOSITORY_ROOT / "fixtures/exchange-profiles/synthetic-gamry-echemdb-v1.json"
)
RELEASED_AT: Final = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)
REPRODUCTION_COMMAND: Final = (
    "python scripts/reproduce_echemdb_cv_exchange.py --output build/echemdb-cv-exchange\n"
    "labbridge validate-artifacts --bundle build/echemdb-cv-exchange\n"
)
LIMITATIONS: Final = """# EchemDB CV exchange limitations

This candidate validates one project-owned `synthetic + replay` CV package against EchemDB
metadata-schema 0.8.3 at commit `f48f583f83b1de9f5601d05dae5e5fcd1c25a3f0`, the
Frictionless Data Package 2.0 profile, and Frictionless 5.19.0. It makes no compatibility claim for
other EchemDB schema, Data Package profile, or Frictionless versions.

Required EchemDB values absent from the DTA bytes are supplied only by the explicit `user_supplied`
assertions in `exchange-profile.json`. Their trace qualifier states that they are not
source-declared. No inferred assertion is projected as source metadata. Semantic EchemDB categories
are marked `fixture_declaration` in the mapping report: they define the project-owned synthetic
fixture and are not independently established as properties of a physical system.

The adapter applies no potential-reference conversion, current normalisation, sign-convention
change, area calculation, electrolyte-composition interpretation, electrode-role assignment, scan
rate derivation, or cycle interpretation. The normalised `V`, `A`, `s`, and dimensionless series are
copied without numeric conversion. The source unit `V vs. Ref.` remains in the LabBridge companion;
the reference scale remains unknown. No literature-dependent scientific claim is made.

Unknown reference scale, potential treatment, current basis, electrode role, geometric area,
contact area, scan rate, and cycle information are omitted and listed in `mapping.json`. The
EchemDB figure type is an explicitly asserted lossy projection; LabBridge `data_origin` and
`execution_mode` remain independently represented in `labbridge-provenance.json`.

The capability status is `implemented`; this uncommitted candidate is not evidence of clean-checkout
demonstration, EchemDB ingestion, EchemDB publication, or production deployment.
"""


def _source() -> RetrievedSource:
    data = SOURCE.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    artifact = SourceArtifact(
        source_artifact_id=source_artifact_id(
            sha256=sha256,
            byte_size=len(data),
            media_type="application/vnd.gamry.dta",
        ),
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


def _exchange_experiment(experiment: Experiment) -> tuple[Experiment, dict[str, object]]:
    raw = json.loads(EXCHANGE_PROFILE.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("assertions"), list):
        raise ValueError("exchange profile must contain an assertion list")
    current = experiment
    for item in raw["assertions"]:
        if not isinstance(item, dict):
            raise ValueError("exchange profile assertion must be an object")
        current = add_user_assertion(
            current,
            expected_version=current.version,
            field_name=str(item["field_name"]),
            requirement_class=cast(RequirementClass, item["requirement_class"]),
            transformation="none",
            value=AssertionValue(state="known", value=str(item["value"])),
            evidence_note=str(item["evidence_note"]),
        )
    return current, raw


def _mapping_csv(report: MappingReport) -> bytes:
    stream = io.StringIO(newline="")
    fieldnames = list(report.entries[0].__class__.model_fields)
    writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for entry in report.entries:
        writer.writerow(entry.model_dump(mode="json"))
    return stream.getvalue().encode("utf-8")


def _mapping_markdown(report: MappingReport) -> str:
    lines = [
        "# LabBridge to EchemDB CV mapping",
        "",
        (
            "| LabBridge field | External field | Status | Origin/state | Semantic review | "
            "Loss or omission |"
        ),
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for entry in report.entries:
        external = entry.external_path or "—"
        origin = "/".join(
            item for item in (entry.assertion_origin, entry.value_state) if item is not None
        )
        detail = entry.loss_reason or entry.note
        lines.append(
            f"| `{entry.labbridge_path}` | `{external}` | {entry.status} | "
            f"{origin or '—'} | {entry.semantic_review} | {detail.replace('|', '\\|')} |"
        )
    lines.extend(
        [
            "",
            (
                "The machine-readable authority is `mapping.json`; `mapping.csv` contains "
                "the same rows."
            ),
        ]
    )
    return "\n".join(lines) + "\n"


def reproduce_echemdb_cv_exchange_artifact(
    destination: Path,
    *,
    producing_version: str,
) -> dict[str, object]:
    """Build and verify the deterministic Phase 6 candidate artifact."""
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"refusing to replace non-empty artifact directory: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    source = _source()
    profile = CVImportProfile.model_validate_json(PROFILE.read_text(encoding="utf-8"))
    normalisation = normalise_cv(
        source,
        profile,
        producing_version=producing_version,
        source_format="gamry_dta",
    )
    experiment, exchange_profile = _exchange_experiment(
        experiment_from_normalisation(normalisation)
    )
    exchange = build_cv_exchange(
        experiment=experiment,
        observation=normalisation.observation,
        source_artifact=source.artifact,
    )
    validation = validate_exchange(exchange)
    if not validation.valid:
        raise ValueError(f"pinned external validation failed: {'; '.join(validation.errors)}")

    (destination / "datapackage.json").write_bytes(canonical_json(exchange.descriptor))
    (destination / "cv.csv").write_bytes(exchange.csv_bytes)
    (destination / "labbridge-provenance.json").write_bytes(canonical_json(exchange.provenance))
    (destination / "mapping.json").write_bytes(
        canonical_json(exchange.report.model_dump(mode="json"))
    )
    (destination / "mapping.csv").write_bytes(_mapping_csv(exchange.report))
    (destination / "MAPPING.md").write_text(
        _mapping_markdown(exchange.report), encoding="utf-8", newline="\n"
    )
    (destination / "validation.json").write_bytes(
        canonical_json(validation.model_dump(mode="json"))
    )
    (destination / "external-versions.json").write_bytes(
        canonical_json(
            {
                "schema_version": "1",
                "adapter": ADAPTER_VERSION,
                "echemdb_metadata_schema": {
                    "version": ECHEMDB_SCHEMA_VERSION,
                    "commit": ECHEMDB_SCHEMA_COMMIT,
                    "sha256": validation.schema_sha256["echemdb_package.json"],
                },
                "data_package_profile": {
                    "version": DATA_PACKAGE_PROFILE_VERSION,
                    "datapackage_sha256": validation.schema_sha256["datapackage.json"],
                    "dataresource_sha256": validation.schema_sha256["dataresource.json"],
                },
                "validators": {
                    "frictionless": FRICTIONLESS_VERSION,
                    "jsonschema": JSONSCHEMA_VERSION,
                    "referencing": REFERENCING_VERSION,
                },
                "compatibility_scope": (
                    "Validated only for the exact versions and schema bytes recorded here."
                ),
            }
        )
    )
    (destination / "exchange-profile.json").write_bytes(canonical_json(exchange_profile))
    (destination / "experiment.json").write_bytes(
        canonical_json(experiment.model_dump(mode="json"))
    )
    (destination / "normalised-observation.json").write_bytes(
        canonical_json(normalisation.observation.model_dump(mode="json"))
    )
    (destination / "transformation-graph.json").write_bytes(
        canonical_json(normalisation.graph.model_dump(mode="json"))
    )
    (destination / "source-artifact.json").write_bytes(
        canonical_json({"schema_version": "1", **source.artifact.model_dump(mode="json")})
    )
    (destination / source.artifact.filename).write_bytes(source.data)
    (destination / "LIMITATIONS.md").write_text(LIMITATIONS, encoding="utf-8", newline="\n")
    (destination / "REPRODUCE.txt").write_text(REPRODUCTION_COMMAND, encoding="utf-8", newline="\n")
    build_manifest(
        destination,
        metadata={
            "artifact_kind": "echemdb_cv_exchange",
            "schema_version": "1",
            "capability_status": "implemented",
            "producing_versions": {
                "labbridge": producing_version,
                "adapter": ADAPTER_VERSION,
                "echemdb_metadata_schema": ECHEMDB_SCHEMA_VERSION,
                "data_package_profile": DATA_PACKAGE_PROFILE_VERSION,
                "frictionless": FRICTIONLESS_VERSION,
            },
            "data_origin": source.artifact.data_origin,
            "execution_mode": source.artifact.execution_mode,
            "source_artifact_id": source.artifact.source_artifact_id,
            "observation_id": normalisation.observation.observation_id,
            "experiment_id": experiment.experiment_id,
            "description": (
                "Version-scoped EchemDB-aligned CV exchange with explicit user assertions, "
                "field traces, loss reporting, and pinned offline validation."
            ),
        },
    )
    return verify_manifest(destination)


__all__ = ["reproduce_echemdb_cv_exchange_artifact"]

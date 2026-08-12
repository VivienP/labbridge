"""Closed, deterministic Experiment Packages and independent offline verification."""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from labbridge.domain.canonical import content_id
from labbridge.domain.cv import CVImportProfile, import_profile_id
from labbridge.domain.cv_observations import (
    NormalisedCVObservation,
    TransformationGraph,
    normalised_observation_id,
    normalised_series_id,
    transformation_record_id,
)
from labbridge.domain.identity import DataOrigin, ExecutionMode
from labbridge.domain.source_artifacts import source_artifact_id

from .manifest import canonical_json, digest
from .passport import ExperimentPassport, render_passport_html, render_passport_json

PACKAGE_SCHEMA_VERSION = "1"
MANIFEST_MEMBER = "manifest.json"


class PackageInputs(BaseModel):
    """Retained Phase 1-2 evidence required to close one Passport package."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    source_filename: str = Field(min_length=1)
    source_bytes: bytes
    source_artifact: dict[str, object]
    import_profile: dict[str, object]
    normalised_observation: dict[str, object]
    transformation_graph: dict[str, object]
    passport: ExperimentPassport


class ExperimentPackage(BaseModel):
    """Versioned identity and checksum metadata for one immutable package archive."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    package_id: str = Field(min_length=1)
    schema_version: Literal["1"]
    passport_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    experiment_version: int = Field(ge=1)
    data_origin: DataOrigin
    execution_mode: ExecutionMode
    environment_id: str = Field(min_length=1)
    supersedes_package_id: str | None = None
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_byte_size: int = Field(ge=1)
    producing_versions: dict[str, str]


@dataclass(frozen=True)
class BuiltExperimentPackage:
    metadata: ExperimentPackage
    archive_bytes: bytes

    @property
    def package_id(self) -> str:
        return self.metadata.package_id

    @property
    def passport_id(self) -> str:
        return self.metadata.passport_id


class PackageVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verified: Literal[True]
    package_id: str
    passport_id: str
    experiment_id: str
    experiment_version: int
    data_origin: DataOrigin
    execution_mode: ExecutionMode
    environment_id: str
    archive_sha256: str
    lineage_closed: Literal[True]


class ExperimentPackageVerificationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _member_entry(name: str, data: bytes) -> dict[str, object]:
    return {"name": name, "sha256": digest(data), "byte_size": len(data)}


def _zip(members: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, members[name])
    return output.getvalue()


def _source_member_name(filename: str) -> str:
    name = PurePosixPath(filename).name
    if name in {"", ".", ".."} or name != filename.replace("\\", "/"):
        raise ValueError("source filename must be a plain filename")
    return f"source/{name}"


def _lineage_payload(inputs: PackageInputs) -> dict[str, object]:
    passport = inputs.passport
    return {
        "schema_version": "1",
        "source_artifact_id": passport.source_artifact_id,
        "observation_id": passport.observation_id,
        "import_profile_id": passport.import_profile_id,
        "transformation_ids": list(passport.transformation_ids),
        "assertions": [
            {
                "assertion_id": item.assertion_id,
                "origin": item.origin,
                "evidence_ids": list(item.evidence_ids),
                "supplements_assertion_id": item.supplements_assertion_id,
                "supersedes_assertion_id": item.supersedes_assertion_id,
            }
            for item in passport.assertions
        ],
        "findings": [
            {
                "finding_id": item.finding_id,
                "assertion_ids": list(item.assertion_ids),
                "evidence_ids": list(item.evidence_ids),
            }
            for item in passport.findings
        ],
    }


def build_experiment_package(
    inputs: PackageInputs,
    *,
    producing_versions: dict[str, str],
    supersedes_package_id: str | None = None,
) -> BuiltExperimentPackage:
    """Build a closed ZIP whose manifest covers every released member."""
    passport = inputs.passport
    if passport.release_status != "released":
        raise ValueError("an Experiment Package requires a released Passport")
    members = {
        _source_member_name(inputs.source_filename): inputs.source_bytes,
        "phase1/source-artifact.json": canonical_json(inputs.source_artifact),
        "phase2/import-profile.json": canonical_json(inputs.import_profile),
        "phase2/normalised-observation.json": canonical_json(inputs.normalised_observation),
        "phase2/transformation-graph.json": canonical_json(inputs.transformation_graph),
        "passport/passport.json": render_passport_json(passport),
        "passport/passport.html": render_passport_html(passport),
        "passport/validation-findings.json": canonical_json(
            [item.model_dump(mode="json") for item in passport.findings]
        ),
        "lineage.json": canonical_json(_lineage_payload(inputs)),
    }
    entries = [_member_entry(name, data) for name, data in sorted(members.items())]
    core: dict[str, object] = {
        "artifact_kind": "experiment_package",
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "passport_id": passport.passport_id,
        "experiment_id": passport.experiment_id,
        "experiment_version": passport.experiment_version,
        "data_origin": passport.data_origin,
        "execution_mode": passport.execution_mode,
        "environment_id": passport.environment_id,
        "supersedes_package_id": supersedes_package_id,
        "producing_versions": dict(sorted(producing_versions.items())),
        "members": entries,
        "members_digest": digest(canonical_json(entries)),
    }
    package_id = content_id("experiment-package", core)
    manifest = {**core, "package_id": package_id}
    members[MANIFEST_MEMBER] = canonical_json(manifest)
    archive_bytes = _zip(members)
    metadata = ExperimentPackage(
        package_id=package_id,
        schema_version=PACKAGE_SCHEMA_VERSION,
        passport_id=passport.passport_id,
        experiment_id=passport.experiment_id,
        experiment_version=passport.experiment_version,
        data_origin=passport.data_origin,
        execution_mode=passport.execution_mode,
        environment_id=passport.environment_id,
        supersedes_package_id=supersedes_package_id,
        archive_sha256=digest(archive_bytes),
        archive_byte_size=len(archive_bytes),
        producing_versions=dict(sorted(producing_versions.items())),
    )
    return BuiltExperimentPackage(metadata=metadata, archive_bytes=archive_bytes)


def _open_members(package_bytes: bytes) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(package_bytes), "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ExperimentPackageVerificationError(
                    "package_member_duplicate", "the archive contains duplicate member names"
                )
            for name in names:
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or "\\" in name:
                    raise ExperimentPackageVerificationError(
                        "package_member_path_invalid", f"unsafe archive member {name!r}"
                    )
            return {name: archive.read(name) for name in names}
    except zipfile.BadZipFile as error:
        raise ExperimentPackageVerificationError(
            "package_archive_invalid", "the package is not a valid ZIP archive"
        ) from error


def _manifest(members: dict[str, bytes]) -> dict[str, object]:
    raw = members.get(MANIFEST_MEMBER)
    if raw is None:
        raise ExperimentPackageVerificationError(
            "package_manifest_missing", "manifest.json is missing"
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ExperimentPackageVerificationError(
            "package_manifest_invalid", "manifest.json is not valid JSON"
        ) from error
    if not isinstance(parsed, dict):
        raise ExperimentPackageVerificationError(
            "package_manifest_invalid", "manifest.json must contain an object"
        )
    return parsed


def _verify_members(members: dict[str, bytes], manifest: dict[str, object]) -> None:
    entries = manifest.get("members")
    if not isinstance(entries, list):
        raise ExperimentPackageVerificationError(
            "package_manifest_invalid", "manifest has no member list"
        )
    if digest(canonical_json(entries)) != manifest.get("members_digest"):
        raise ExperimentPackageVerificationError(
            "package_manifest_digest_mismatch", "members_digest does not match member entries"
        )
    listed: set[str] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            raise ExperimentPackageVerificationError(
                "package_manifest_invalid", "manifest member entry is not an object"
            )
        name = str(raw.get("name", ""))
        listed.add(name)
        data = members.get(name)
        if data is None:
            raise ExperimentPackageVerificationError(
                "package_member_missing", f"{name} is listed but missing"
            )
        if digest(data) != raw.get("sha256"):
            raise ExperimentPackageVerificationError(
                "package_member_sha256_mismatch", f"{name} does not match its SHA-256"
            )
        if len(data) != raw.get("byte_size"):
            raise ExperimentPackageVerificationError(
                "package_member_size_mismatch", f"{name} does not match its byte size"
            )
    present = set(members) - {MANIFEST_MEMBER}
    extras = sorted(present - listed)
    if extras:
        raise ExperimentPackageVerificationError(
            "package_member_unexpected", f"unexpected member {extras[0]}"
        )


def _json_member(members: dict[str, bytes], name: str) -> dict[str, object]:
    try:
        parsed = json.loads(members[name])
    except (KeyError, json.JSONDecodeError) as error:
        raise ExperimentPackageVerificationError(
            "package_member_invalid", f"{name} is missing or invalid JSON"
        ) from error
    if not isinstance(parsed, dict):
        raise ExperimentPackageVerificationError(
            "package_member_invalid", f"{name} must contain a JSON object"
        )
    return parsed


def _verify_phase_evidence_identity(  # noqa: PLR0912 - each branch rejects one identity defect
    source: dict[str, object],
    observation: dict[str, object],
    profile: dict[str, object],
    graph: dict[str, object],
    passport: ExperimentPassport,
) -> None:
    try:
        profile_body = {key: value for key, value in profile.items() if key != "profile_id"}
        profile_model = CVImportProfile.model_validate(profile_body)
        observation_model = NormalisedCVObservation.model_validate(observation)
        graph_model = TransformationGraph.model_validate(graph)
    except ValueError as error:
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "retained Phase 2 evidence does not match its schema"
        ) from error
    source_sha256 = source.get("sha256")
    source_byte_size = source.get("byte_size")
    source_media_type = source.get("media_type")
    if (
        not isinstance(source_sha256, str)
        or not isinstance(source_byte_size, int)
        or not isinstance(source_media_type, str)
    ):
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "Phase 1 source identity inputs are invalid"
        )
    expected_source_id = source_artifact_id(
        sha256=source_sha256,
        byte_size=source_byte_size,
        media_type=source_media_type,
    )
    if source.get("source_artifact_id") != expected_source_id:
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "Phase 1 source identity does not match retained content"
        )
    if profile.get("profile_id") != import_profile_id(profile_model):
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "Phase 2 profile identity does not match retained content"
        )
    for series in observation_model.series:
        expected_series_id = normalised_series_id(
            source_artifact_id=observation_model.source_artifact_id,
            import_profile_id=observation_model.import_profile_id,
            schema_version=series.schema_version,
            dtype=series.dtype,
            shape=series.shape,
            source_column=series.source_column,
            role=series.role,
            source_unit=series.source_unit,
            unit=series.unit,
            values=series.values,
        )
        if series.series_id != expected_series_id:
            raise ExperimentPackageVerificationError(
                "package_lineage_open", "Phase 2 series identity does not match retained content"
            )
    if observation_model.observation_id != normalised_observation_id(observation_model):
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "Phase 2 observation identity does not match retained content"
        )
    if any(
        record.transformation_id != transformation_record_id(record)
        for record in graph_model.records
    ):
        raise ExperimentPackageVerificationError(
            "package_lineage_open",
            "Phase 2 transformation identity does not match retained content",
        )
    graph_ids = tuple(record.transformation_id for record in graph_model.records)
    if observation_model.transformation_ids != graph_ids:
        raise ExperimentPackageVerificationError(
            "package_lineage_open",
            "Phase 2 observation and transformation graph identities differ",
        )
    if observation_model.provenance.transformation_ids != graph_ids:
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "Phase 2 provenance omits retained transformations"
        )
    if (
        observation_model.provenance.source_artifact_id != observation_model.source_artifact_id
        or observation_model.provenance.import_profile_id != observation_model.import_profile_id
        or observation_model.provenance.environment_id != observation_model.environment_id
        or observation_model.provenance.source_sha256 != source.get("sha256")
    ):
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "Phase 2 provenance differs from retained evidence"
        )
    graph_record_ids = {record.transformation_id for record in graph_model.records}
    if any(series.transformation_id not in graph_record_ids for series in observation_model.series):
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "Phase 2 series names an absent transformation"
        )
    if source.get("source_artifact_id") != passport.source_artifact_id:
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "Passport and Phase 1 source identities differ"
        )
    if observation.get("observation_id") != passport.observation_id:
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "Passport and Phase 2 observation identities differ"
        )
    if observation.get("source_artifact_id") != passport.source_artifact_id:
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "Phase 2 observation does not name the Phase 1 source"
        )
    if profile.get("profile_id") != passport.import_profile_id:
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "Passport and Phase 2 profile identities differ"
        )
    for field_name in ("data_origin", "execution_mode"):
        expected = getattr(passport, field_name)
        if source.get(field_name) != expected or observation.get(field_name) != expected:
            raise ExperimentPackageVerificationError(
                "package_origin_mismatch",
                f"Phase 1, Phase 2, and Passport {field_name} values differ",
            )
    if observation.get("environment_id") != passport.environment_id:
        raise ExperimentPackageVerificationError(
            "package_origin_mismatch",
            "Phase 2 and Passport environment identities differ",
        )
    if profile.get("environment_id") != passport.environment_id:
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "Phase 2 profile names another environment"
        )


def _verify_lineage(  # noqa: PLR0912 - each branch rejects one distinct lineage defect
    members: dict[str, bytes], passport: ExperimentPassport
) -> None:
    source = _json_member(members, "phase1/source-artifact.json")
    source_members = [name for name in members if name.startswith("source/")]
    if len(source_members) != 1:
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "the package must contain exactly one retained source member"
        )
    source_bytes = members[source_members[0]]
    if source.get("sha256") != digest(source_bytes) or source.get("byte_size") != len(source_bytes):
        raise ExperimentPackageVerificationError(
            "package_source_integrity_mismatch", "retained source bytes differ from their record"
        )
    observation = _json_member(members, "phase2/normalised-observation.json")
    profile = _json_member(members, "phase2/import-profile.json")
    graph = _json_member(members, "phase2/transformation-graph.json")
    lineage = _json_member(members, "lineage.json")
    _verify_phase_evidence_identity(source, observation, profile, graph, passport)
    available = {passport.source_artifact_id}
    records = graph.get("records")
    if not isinstance(records, list):
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "transformation graph has no records"
        )
    transformation_ids: set[str] = set()
    for raw in records:
        if not isinstance(raw, dict):
            raise ExperimentPackageVerificationError(
                "package_lineage_open", "transformation record is not an object"
            )
        inputs = raw.get("input_ids")
        outputs = raw.get("output_ids")
        transformation_id = raw.get("transformation_id")
        if (
            not isinstance(inputs, list)
            or not isinstance(outputs, list)
            or not isinstance(transformation_id, str)
            or not set(map(str, inputs)).issubset(available)
        ):
            raise ExperimentPackageVerificationError(
                "package_lineage_open", "transformation inputs do not close to retained evidence"
            )
        transformation_ids.add(transformation_id)
        available.update(map(str, outputs))
    if passport.observation_id not in available:
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "transformation graph does not reach the observation"
        )
    if not set(passport.transformation_ids).issubset(transformation_ids):
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "Passport transformations are absent from the Phase 2 graph"
        )
    assertion_ids = {item.assertion_id for item in passport.assertions}
    evidence_universe = {
        passport.source_artifact_id,
        passport.observation_id,
        passport.import_profile_id,
        passport.experiment_id,
        *transformation_ids,
        *assertion_ids,
    }
    for assertion in passport.assertions:
        if not set(assertion.evidence_ids).issubset(evidence_universe):
            raise ExperimentPackageVerificationError(
                "package_lineage_open",
                f"assertion {assertion.assertion_id} names evidence absent from the package",
            )
        if assertion.origin == "user_supplied" and not assertion.evidence_note:
            raise ExperimentPackageVerificationError(
                "package_lineage_open",
                f"user assertion {assertion.assertion_id} has no retained evidence note",
            )
    for finding in passport.findings:
        if not set(finding.assertion_ids).issubset(assertion_ids):
            raise ExperimentPackageVerificationError(
                "package_lineage_open", f"finding {finding.finding_id} names an absent assertion"
            )
        if not set(finding.evidence_ids).issubset(evidence_universe):
            raise ExperimentPackageVerificationError(
                "package_lineage_open", f"finding {finding.finding_id} names absent evidence"
            )
    if lineage.get("source_artifact_id") != passport.source_artifact_id:
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "lineage inventory does not name the retained source"
        )


def verify_experiment_package(package_bytes: bytes) -> PackageVerification:
    """Verify package closure, report parity, and lineage without a database or object store."""
    members = _open_members(package_bytes)
    manifest = _manifest(members)
    _verify_members(members, manifest)
    core = {key: value for key, value in manifest.items() if key != "package_id"}
    expected_package_id = content_id("experiment-package", core)
    if manifest.get("package_id") != expected_package_id:
        raise ExperimentPackageVerificationError(
            "package_identity_mismatch", "package_id does not match the canonical manifest"
        )
    try:
        passport = ExperimentPassport.model_validate_json(members["passport/passport.json"])
    except (KeyError, ValueError) as error:
        raise ExperimentPackageVerificationError(
            "package_passport_invalid", "passport/passport.json is invalid"
        ) from error
    if passport.passport_id != manifest.get("passport_id"):
        raise ExperimentPackageVerificationError(
            "package_passport_mismatch", "Passport and manifest identities differ"
        )
    for field_name in ("data_origin", "execution_mode", "environment_id"):
        if manifest.get(field_name) != getattr(passport, field_name):
            raise ExperimentPackageVerificationError(
                "package_origin_mismatch",
                f"Package manifest and Passport {field_name} values differ",
            )
    html_report = members.get("passport/passport.html", b"").decode("utf-8", errors="replace")
    if f"Release decision: {passport.release_decision.status}" not in html_report:
        raise ExperimentPackageVerificationError(
            "package_report_contract_mismatch", "HTML and JSON release decisions differ"
        )
    for finding_id in passport.release_decision.finding_ids:
        if finding_id not in html_report:
            raise ExperimentPackageVerificationError(
                "package_report_contract_mismatch", f"HTML omits finding {finding_id}"
            )
    _verify_lineage(members, passport)
    return PackageVerification(
        verified=True,
        package_id=expected_package_id,
        passport_id=passport.passport_id,
        experiment_id=passport.experiment_id,
        experiment_version=passport.experiment_version,
        data_origin=passport.data_origin,
        execution_mode=passport.execution_mode,
        environment_id=passport.environment_id,
        archive_sha256=digest(package_bytes),
        lineage_closed=True,
    )


__all__ = [
    "PACKAGE_SCHEMA_VERSION",
    "BuiltExperimentPackage",
    "ExperimentPackage",
    "ExperimentPackageVerificationError",
    "PackageInputs",
    "PackageVerification",
    "build_experiment_package",
    "verify_experiment_package",
]

"""Closed, deterministic Experiment Packages and independent offline verification."""

from __future__ import annotations

import importlib
import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from labbridge.domain.canonical import content_id
from labbridge.domain.cv import CVImportProfile, import_profile_id
from labbridge.domain.cv_observations import (
    NormalisedCVObservation,
    NormalisedSeries,
    TransformationGraph,
    normalised_observation_id,
    normalised_series_id,
    transformation_record_id,
)
from labbridge.domain.electrolysis import (
    AuxiliaryAnalyticalResult,
    ElectrolysisColumnRole,
    ElectrolysisImportProfile,
    auxiliary_result_id,
    electrolysis_import_profile_id,
)
from labbridge.domain.electrolysis_observations import (
    NormalisedElectrolysisObservation,
    electrolysis_observation_id,
    electrolysis_series_id,
)
from labbridge.domain.experiments import (
    AssertionOrigin,
    AssertionTransformation,
    MetadataAssertion,
    RequirementClass,
)
from labbridge.domain.identity import DataOrigin, ExecutionMode
from labbridge.domain.parser_diagnostics import ParserRecord
from labbridge.domain.source_artifacts import source_artifact_id
from labbridge.infrastructure.objectstore import ObjectStore

from .manifest import canonical_json, digest
from .passport import ExperimentPassport, render_passport_html, render_passport_json

if TYPE_CHECKING:
    from .campaign_package import CampaignPackageVerification

PACKAGE_SCHEMA_VERSION = "2"
LEGACY_PACKAGE_SCHEMA_VERSION = "1"
ELECTROLYSIS_PACKAGE_SCHEMA_VERSION = "3"
MANIFEST_MEMBER = "manifest.json"
MAX_PACKAGE_MEMBERS = 128
MAX_PACKAGE_MEMBER_SIZE = 32 * 1024 * 1024
MAX_PACKAGE_UNCOMPRESSED_SIZE = 64 * 1024 * 1024
MAX_PACKAGE_COMPRESSION_RATIO = 100

_ELECTROLYSIS_PROFILE_ASSERTION_REQUIREMENTS: dict[str, RequirementClass] = {
    "source_artifact": "required",
    "observation": "required",
    "time_axis": "required",
    "potential_axis": "required",
    "current_axis": "required",
    "current_quantity_kind": "required",
    "current_sign_convention": "conditional",
    "current_basis": "conditional",
    "electrode_area": "conditional",
    "cell_geometry": "recommended",
    "reference_scale": "conditional",
    "potential_treatment": "conditional",
    "sampling_interval": "recommended",
    "interruptions": "recommended",
    "chemical_analysis": "optional",
    "scan_rate": "optional",
    "cycle_information": "optional",
}


class AuxiliaryPackageSource(BaseModel):
    """Exact bytes and Phase 1 record for one linked analytical source."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    source_filename: str = Field(min_length=1)
    source_bytes: bytes
    source_artifact: dict[str, object]


class PackageInputs(BaseModel):
    """Retained Phase 1-2 evidence required to close one Passport package."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    source_filename: str = Field(min_length=1)
    source_bytes: bytes
    source_artifact: dict[str, object]
    import_profile: dict[str, object]
    normalised_observation: dict[str, object]
    transformation_graph: dict[str, object]
    parser_record: dict[str, object] | None = None
    auxiliary_sources: tuple[AuxiliaryPackageSource, ...] = ()
    passport: ExperimentPassport


class ExperimentPackage(BaseModel):
    """Versioned identity and checksum metadata for one immutable package archive."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    package_id: str = Field(min_length=1)
    schema_version: Literal["1", "2", "3"]
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


def _auxiliary_source_member_name(source_artifact_id_value: str, filename: str) -> str:
    name = PurePosixPath(filename).name
    if name in {"", ".", ".."} or name != filename.replace("\\", "/"):
        raise ValueError("auxiliary source filename must be a plain filename")
    safe_identity = source_artifact_id_value.replace(":", "-")
    if "/" in safe_identity or "\\" in safe_identity:
        raise ValueError("auxiliary source identity is not path safe")
    return f"auxiliary-source/{safe_identity}/{name}"


def _lineage_payload(inputs: PackageInputs) -> dict[str, object]:
    passport = inputs.passport
    payload: dict[str, object] = {
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
    if inputs.parser_record is not None:
        payload["parser_record_id"] = inputs.parser_record.get("parser_record_id")
    auxiliary_results = inputs.normalised_observation.get("auxiliary_results", [])
    if passport.technique == "galvanostatic_electrolysis" and isinstance(auxiliary_results, list):
        payload["auxiliary_result_ids"] = [
            item.get("result_id") for item in auxiliary_results if isinstance(item, dict)
        ]
        payload["auxiliary_source_artifact_ids"] = sorted(
            {
                str(item.get("source_artifact_id"))
                for item in auxiliary_results
                if isinstance(item, dict)
            }
        )
    return payload


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
    if passport.technique == "galvanostatic_electrolysis":
        if inputs.parser_record is not None:
            raise ValueError("generic electrolysis Packages cannot carry a vendor parser record")
        raw_results = inputs.normalised_observation.get("auxiliary_results")
        if not isinstance(raw_results, list):
            raise ValueError("electrolysis observation has no auxiliary result inventory")
        expected_auxiliary = {
            str(item.get("source_artifact_id")) for item in raw_results if isinstance(item, dict)
        }
        supplied_auxiliary = {
            str(item.source_artifact.get("source_artifact_id")) for item in inputs.auxiliary_sources
        }
        if expected_auxiliary != supplied_auxiliary:
            raise ValueError("auxiliary result sources differ from packaged source artifacts")
        auxiliary_inventory: list[dict[str, object]] = []
        for item in sorted(
            inputs.auxiliary_sources,
            key=lambda source: str(source.source_artifact.get("source_artifact_id")),
        ):
            source_id_value = str(item.source_artifact.get("source_artifact_id"))
            member_name = _auxiliary_source_member_name(source_id_value, item.source_filename)
            members[member_name] = item.source_bytes
            auxiliary_inventory.append(
                {"member_name": member_name, "source_artifact": item.source_artifact}
            )
        members["phase1/auxiliary-source-artifacts.json"] = canonical_json(auxiliary_inventory)
        schema_version = ELECTROLYSIS_PACKAGE_SCHEMA_VERSION
    elif inputs.auxiliary_sources:
        raise ValueError("CV Packages cannot carry electrolysis auxiliary sources")
    else:
        schema_version = (
            PACKAGE_SCHEMA_VERSION
            if inputs.parser_record is not None
            else LEGACY_PACKAGE_SCHEMA_VERSION
        )
    if inputs.parser_record is not None:
        members["phase2/parser-record.json"] = canonical_json(inputs.parser_record)
    entries = [_member_entry(name, data) for name, data in sorted(members.items())]
    core: dict[str, object] = {
        "artifact_kind": "experiment_package",
        "schema_version": schema_version,
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
        schema_version=schema_version,
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
            infos = archive.infolist()
            total_size = sum(info.file_size for info in infos)
            limits_exceeded = (
                len(infos) > MAX_PACKAGE_MEMBERS
                or total_size > MAX_PACKAGE_UNCOMPRESSED_SIZE
                or any(info.file_size > MAX_PACKAGE_MEMBER_SIZE for info in infos)
                or any(
                    info.file_size > 0
                    and info.file_size / max(info.compress_size, 1) > MAX_PACKAGE_COMPRESSION_RATIO
                    for info in infos
                )
            )
            if limits_exceeded:
                raise ExperimentPackageVerificationError(
                    "package_archive_limits_exceeded",
                    "the archive exceeds a member, size, or compression-ratio limit",
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
        if name in listed:
            raise ExperimentPackageVerificationError(
                "package_manifest_invalid", f"manifest repeats member {name}"
            )
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


def _verify_electrolysis_profile_semantics(
    profile_id: object,
    profile: ElectrolysisImportProfile,
    observation: NormalisedElectrolysisObservation,
) -> None:
    if (
        observation.import_profile_id != profile_id
        or observation.metadata != profile.metadata
        or observation.auxiliary_results != profile.auxiliary_results
    ):
        raise ExperimentPackageVerificationError(
            "package_lineage_open",
            "electrolysis observation semantics differ from the retained profile",
        )
    profile_mappings = {
        (item.source_column, item.role, item.source_unit, item.target_unit)
        for item in profile.columns
        if item.role != "ignored"
    }
    if any(
        (series.source_column, series.role, series.source_unit, series.unit) not in profile_mappings
        for series in observation.series
    ):
        raise ExperimentPackageVerificationError(
            "package_lineage_open",
            "electrolysis series semantics differ from the retained profile mapping",
        )


def _active_electrolysis_profile_assertions(
    passport: ExperimentPassport,
) -> dict[str, MetadataAssertion]:
    active_ids = passport.active_assertion_ids
    active_id_set = set(active_ids)
    active = [item for item in passport.assertions if item.assertion_id in active_id_set]
    protected_fields = set(_ELECTROLYSIS_PROFILE_ASSERTION_REQUIREMENTS)
    protected = [item for item in active if item.field_name in protected_fields]
    counts = {field_name: 0 for field_name in protected_fields}
    for assertion in protected:
        counts[assertion.field_name] += 1
    if (
        len(active_ids) != len(active_id_set)
        or len(active) != len(active_ids)
        or any(count != 1 for count in counts.values())
    ):
        raise ExperimentPackageVerificationError(
            "package_lineage_open",
            "electrolysis Passport requires one active assertion per profile-owned field",
        )
    unsupported_known = next(
        (
            item
            for item in active
            if item.field_name not in protected_fields
            and not item.field_name.startswith("auxiliary_result.")
            and item.value.state == "known"
        ),
        None,
    )
    if unsupported_known is not None:
        raise ExperimentPackageVerificationError(
            "package_lineage_open",
            f"active electrolysis claim {unsupported_known.field_name} has no approved contract",
        )
    return {item.field_name: item for item in protected}


def _verify_electrolysis_assertion_authority(
    observation: NormalisedElectrolysisObservation,
    assertions: dict[str, MetadataAssertion],
) -> None:
    common_evidence = (
        observation.source_artifact_id,
        observation.import_profile_id,
        *observation.transformation_ids,
    )
    expected: dict[
        str,
        tuple[
            RequirementClass,
            AssertionOrigin,
            AssertionTransformation,
            tuple[str, ...],
        ],
    ] = {
        "source_artifact": (
            "required",
            "source_file",
            "none",
            (observation.source_artifact_id,),
        ),
        "observation": (
            "required",
            "source_file",
            "derived",
            (observation.source_artifact_id, *observation.transformation_ids),
        ),
    }
    for series in observation.series:
        field_name = (
            "current_axis"
            if series.role in {"current", "current_density"}
            else f"{series.role}_axis"
        )
        series_evidence = (
            observation.source_artifact_id,
            observation.import_profile_id,
            series.transformation_id,
        )
        expected[field_name] = (
            "required",
            "user_supplied",
            "parsed" if series.source_unit == series.unit else "unit_converted",
            series_evidence,
        )
        if series.role in {"current", "current_density"}:
            expected["current_quantity_kind"] = (
                "required",
                "user_supplied",
                "none",
                series_evidence,
            )
    for field_name, requirement_class in _ELECTROLYSIS_PROFILE_ASSERTION_REQUIREMENTS.items():
        if field_name not in expected:
            expected[field_name] = (
                requirement_class,
                "user_supplied",
                "none",
                common_evidence,
            )
    for field_name, assertion in assertions.items():
        requirement, origin, transformation, evidence_ids = expected[field_name]
        assertion_body = assertion.model_dump(mode="python", exclude={"assertion_id"})
        if (
            assertion.requirement_class != requirement
            or assertion.origin != origin
            or assertion.transformation != transformation
            or assertion.evidence_ids != evidence_ids
            or assertion.assertion_id != content_id("assertion", assertion_body)
        ):
            raise ExperimentPackageVerificationError(
                "package_lineage_open",
                f"Passport {field_name} authority differs from retained electrolysis evidence",
            )


def _verify_electrolysis_passport_semantics(
    observation: NormalisedElectrolysisObservation,
    passport: ExperimentPassport,
) -> None:
    assertions = _active_electrolysis_profile_assertions(passport)
    _verify_electrolysis_assertion_authority(observation, assertions)
    series_by_role: dict[str, NormalisedSeries] = {item.role: item for item in observation.series}
    for field_name, role in (
        ("time_axis", "time"),
        ("potential_axis", "potential"),
        ("current_axis", "current" if "current" in series_by_role else "current_density"),
    ):
        assertion = assertions.get(field_name)
        series = series_by_role[role]
        if (
            assertion is None
            or assertion.value.state != "known"
            or assertion.value.value != series.source_column
            or assertion.value.unit != series.unit
        ):
            raise ExperimentPackageVerificationError(
                "package_lineage_open",
                f"Passport {field_name} differs from the retained electrolysis series",
            )
    current_series = series_by_role.get("current") or series_by_role["current_density"]
    current_kind = assertions.get("current_quantity_kind")
    if (
        current_kind is None
        or current_kind.value.state != "known"
        or current_kind.value.value != current_series.role
        or current_kind.value.unit is not None
    ):
        raise ExperimentPackageVerificationError(
            "package_lineage_open",
            "Passport current quantity kind differs from the retained electrolysis series",
        )
    for field_name in (
        "current_sign_convention",
        "current_basis",
        "electrode_area",
        "cell_geometry",
        "reference_scale",
        "potential_treatment",
        "sampling_interval",
        "interruptions",
        "chemical_analysis",
    ):
        assertion = assertions.get(field_name)
        metadata = getattr(observation.metadata, field_name)
        if assertion is None or assertion.value.model_dump() != metadata.model_dump():
            raise ExperimentPackageVerificationError(
                "package_lineage_open",
                f"Passport {field_name} differs from retained electrolysis metadata",
            )
    for field_name, expected_value in (
        ("source_artifact", observation.source_artifact_id),
        ("observation", observation.observation_id),
    ):
        assertion = assertions.get(field_name)
        if (
            assertion is None
            or assertion.value.state != "known"
            or assertion.value.value != expected_value
            or assertion.value.unit is not None
        ):
            raise ExperimentPackageVerificationError(
                "package_lineage_open",
                f"Passport {field_name} assertion differs from retained electrolysis evidence",
            )
    for field_name in ("scan_rate", "cycle_information"):
        assertion = assertions.get(field_name)
        if assertion is None or assertion.value.state != "not_applicable":
            raise ExperimentPackageVerificationError(
                "package_lineage_open",
                f"Passport {field_name} is not applicable to electrolysis",
            )


def _verify_electrolysis_evidence_identity(  # noqa: PLR0912
    source: dict[str, object],
    observation: dict[str, object],
    profile: dict[str, object],
    graph: dict[str, object],
    passport: ExperimentPassport,
) -> None:
    try:
        profile_body = {key: value for key, value in profile.items() if key != "profile_id"}
        profile_model = ElectrolysisImportProfile.model_validate(profile_body)
        observation_model = NormalisedElectrolysisObservation.model_validate(observation)
        graph_model = TransformationGraph.model_validate(graph)
    except ValueError as error:
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "retained electrolysis evidence does not match its schema"
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
            "package_lineage_open", "primary electrolysis source identity inputs are invalid"
        )
    if source.get("source_artifact_id") != source_artifact_id(
        sha256=source_sha256,
        byte_size=source_byte_size,
        media_type=source_media_type,
    ):
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "primary electrolysis source identity does not match content"
        )
    if profile.get("profile_id") != electrolysis_import_profile_id(profile_model):
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "electrolysis profile identity does not match content"
        )
    _verify_electrolysis_profile_semantics(
        profile.get("profile_id"), profile_model, observation_model
    )
    for series in observation_model.series:
        expected = electrolysis_series_id(
            source_artifact_id=observation_model.source_artifact_id,
            import_profile_id=observation_model.import_profile_id,
            schema_version=series.schema_version,
            dtype=series.dtype,
            shape=series.shape,
            source_column=series.source_column,
            role=cast(ElectrolysisColumnRole, series.role),
            source_unit=series.source_unit,
            unit=series.unit,
            values=series.values,
        )
        if series.series_id != expected:
            raise ExperimentPackageVerificationError(
                "package_lineage_open", "electrolysis series identity does not match content"
            )
    if observation_model.observation_id != electrolysis_observation_id(observation_model):
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "electrolysis observation identity does not match content"
        )
    if any(
        record.transformation_id != transformation_record_id(record)
        for record in graph_model.records
    ):
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "electrolysis transformation identity does not match content"
        )
    graph_ids = tuple(record.transformation_id for record in graph_model.records)
    if (
        observation_model.transformation_ids != graph_ids
        or observation_model.provenance.transformation_ids != graph_ids
    ):
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "electrolysis transformation inventories differ"
        )
    if (
        observation_model.provenance.source_artifact_id != observation_model.source_artifact_id
        or observation_model.provenance.import_profile_id != observation_model.import_profile_id
        or observation_model.provenance.environment_id != observation_model.environment_id
        or observation_model.provenance.source_sha256 != source_sha256
    ):
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "electrolysis provenance differs from retained evidence"
        )
    graph_record_ids = {record.transformation_id for record in graph_model.records}
    if any(series.transformation_id not in graph_record_ids for series in observation_model.series):
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "electrolysis series names an absent transformation"
        )
    if (
        source.get("source_artifact_id") != passport.source_artifact_id
        or observation_model.observation_id != passport.observation_id
        or observation_model.source_artifact_id != passport.source_artifact_id
        or profile.get("profile_id") != passport.import_profile_id
    ):
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "electrolysis Passport lineage anchors differ"
        )
    for field_name in ("data_origin", "execution_mode"):
        expected_value = getattr(passport, field_name)
        if (
            source.get(field_name) != expected_value
            or getattr(observation_model, field_name) != expected_value
        ):
            raise ExperimentPackageVerificationError(
                "package_origin_mismatch",
                f"electrolysis source, observation, and Passport {field_name} differ",
            )
    if (
        observation_model.environment_id != passport.environment_id
        or profile_model.environment_id != passport.environment_id
    ):
        raise ExperimentPackageVerificationError(
            "package_origin_mismatch", "electrolysis environment identities differ"
        )
    _verify_electrolysis_passport_semantics(observation_model, passport)


def _verify_phase_evidence_identity(  # noqa: PLR0912,PLR0915
    source: dict[str, object],
    observation: dict[str, object],
    profile: dict[str, object],
    graph: dict[str, object],
    passport: ExperimentPassport,
) -> None:
    if profile.get("technique") == "galvanostatic_electrolysis":
        _verify_electrolysis_evidence_identity(source, observation, profile, graph, passport)
        return
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


def _verify_parser_evidence(
    *,
    members: dict[str, bytes],
    manifest: dict[str, object],
    source: dict[str, object],
    observation: dict[str, object],
    profile: dict[str, object],
    graph: dict[str, object],
    schema_version: str,
) -> str | None:
    member_name = "phase2/parser-record.json"
    if schema_version == ELECTROLYSIS_PACKAGE_SCHEMA_VERSION:
        if member_name in members or observation.get("parser_record_id") is not None:
            raise ExperimentPackageVerificationError(
                "package_schema_mismatch",
                "electrolysis Package schema 3 cannot carry a vendor parser record",
            )
        versions = manifest.get("producing_versions")
        if (
            not isinstance(versions, dict)
            or versions.get("experiment_package") != ELECTROLYSIS_PACKAGE_SCHEMA_VERSION
        ):
            raise ExperimentPackageVerificationError(
                "package_lineage_open", "Package producing versions omit schema 3"
            )
        return None
    if schema_version == LEGACY_PACKAGE_SCHEMA_VERSION:
        if member_name in members or observation.get("parser_record_id") is not None:
            raise ExperimentPackageVerificationError(
                "package_schema_mismatch",
                "Package schema 1 cannot carry a parser record",
            )
        return None
    parser_body = _json_member(members, member_name)
    try:
        record = ParserRecord.model_validate(parser_body)
    except ValueError as error:
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "the retained parser record does not match its schema"
        ) from error
    if record.status != "accepted":
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "a released Package cannot use a rejected parser record"
        )
    raw_provenance = observation.get("provenance")
    provenance_parser_record_id = (
        raw_provenance.get("parser_record_id") if isinstance(raw_provenance, dict) else None
    )
    if (
        record.source_artifact_id != source.get("source_artifact_id")
        or record.import_profile_id != profile.get("profile_id")
        or record.parser_record_id != observation.get("parser_record_id")
        or record.parser_record_id != provenance_parser_record_id
        or record.parser_version != observation.get("parser_version")
    ):
        raise ExperimentPackageVerificationError(
            "package_lineage_open",
            "parser, source, profile, observation, and provenance identities differ",
        )
    records = graph.get("records")
    if not isinstance(records, list) or not any(
        isinstance(item, dict)
        and item.get("kind") == "dta_parse"
        and record.parser_record_id in item.get("output_ids", [])
        for item in records
    ):
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "the transformation graph omits the parser record"
        )
    versions = manifest.get("producing_versions")
    if not isinstance(versions, dict) or (
        versions.get("experiment_package") != PACKAGE_SCHEMA_VERSION
        or versions.get("gamry_dta_parser") != record.parser_version
    ):
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "Package producing versions omit the DTA parser"
        )
    return record.parser_record_id


def _verify_auxiliary_sources(  # noqa: PLR0912,PLR0915
    *,
    members: dict[str, bytes],
    observation: dict[str, object],
    profile: dict[str, object],
    passport: ExperimentPassport,
    schema_version: str,
) -> tuple[set[str], set[str]]:
    inventory_name = "phase1/auxiliary-source-artifacts.json"
    auxiliary_members = {name for name in members if name.startswith("auxiliary-source/")}
    if schema_version != ELECTROLYSIS_PACKAGE_SCHEMA_VERSION:
        if inventory_name in members or auxiliary_members:
            raise ExperimentPackageVerificationError(
                "package_schema_mismatch", "CV Package carries electrolysis auxiliary sources"
            )
        return set(), set()
    try:
        raw_inventory = json.loads(members[inventory_name])
    except (KeyError, json.JSONDecodeError) as error:
        raise ExperimentPackageVerificationError(
            "package_member_invalid", "auxiliary source inventory is missing or invalid"
        ) from error
    if not isinstance(raw_inventory, list):
        raise ExperimentPackageVerificationError(
            "package_member_invalid", "auxiliary source inventory must contain a list"
        )
    source_ids: set[str] = set()
    listed_members: set[str] = set()
    for raw in raw_inventory:
        if not isinstance(raw, dict):
            raise ExperimentPackageVerificationError(
                "package_member_invalid", "auxiliary source entry is not an object"
            )
        member_name = raw.get("member_name")
        source = raw.get("source_artifact")
        if not isinstance(member_name, str) or not isinstance(source, dict):
            raise ExperimentPackageVerificationError(
                "package_member_invalid", "auxiliary source entry is incomplete"
            )
        data = members.get(member_name)
        if data is None:
            raise ExperimentPackageVerificationError(
                "package_lineage_open", "auxiliary source bytes are absent"
            )
        source_sha256 = source.get("sha256")
        byte_size = source.get("byte_size")
        media_type = source.get("media_type")
        if (
            not isinstance(source_sha256, str)
            or not isinstance(byte_size, int)
            or not isinstance(media_type, str)
            or source_sha256 != digest(data)
            or byte_size != len(data)
        ):
            raise ExperimentPackageVerificationError(
                "package_source_integrity_mismatch",
                "auxiliary source bytes differ from their retained record",
            )
        expected_id = source_artifact_id(
            sha256=source_sha256, byte_size=byte_size, media_type=media_type
        )
        if source.get("source_artifact_id") != expected_id:
            raise ExperimentPackageVerificationError(
                "package_lineage_open", "auxiliary source identity does not match content"
            )
        if (
            source.get("data_origin") != passport.data_origin
            or source.get("execution_mode") != passport.execution_mode
        ):
            raise ExperimentPackageVerificationError(
                "package_origin_mismatch", "auxiliary source origin or mode differs"
            )
        if expected_id in source_ids or member_name in listed_members:
            raise ExperimentPackageVerificationError(
                "package_lineage_open", "auxiliary source inventory contains a duplicate"
            )
        source_ids.add(expected_id)
        listed_members.add(member_name)
    if listed_members != auxiliary_members:
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "auxiliary source inventory and members differ"
        )
    raw_results = observation.get("auxiliary_results")
    profile_results = profile.get("auxiliary_results")
    if not isinstance(raw_results, list) or raw_results != profile_results:
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "profile and observation auxiliary results differ"
        )
    result_ids: set[str] = set()
    referenced_sources: set[str] = set()
    for raw in raw_results:
        if not isinstance(raw, dict):
            raise ExperimentPackageVerificationError(
                "package_lineage_open", "auxiliary result is not an object"
            )
        try:
            result = AuxiliaryAnalyticalResult.model_validate(raw)
        except ValueError as error:
            raise ExperimentPackageVerificationError(
                "package_lineage_open", "auxiliary result does not match its schema"
            ) from error
        if result.result_id != auxiliary_result_id(result):
            raise ExperimentPackageVerificationError(
                "package_lineage_open", "auxiliary result identity does not match content"
            )
        if result.electrical_source_artifact_id != observation.get("source_artifact_id"):
            raise ExperimentPackageVerificationError(
                "package_lineage_open",
                "auxiliary result does not name the retained electrical source",
            )
        result_ids.add(result.result_id)
        referenced_sources.add(result.source_artifact_id)
    if referenced_sources != source_ids:
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "auxiliary results and retained sources differ"
        )
    return source_ids, result_ids


def _verify_lineage(  # noqa: PLR0912,PLR0915
    members: dict[str, bytes],
    passport: ExperimentPassport,
    *,
    manifest: dict[str, object],
    schema_version: str,
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
    auxiliary_source_ids, auxiliary_result_ids = _verify_auxiliary_sources(
        members=members,
        observation=observation,
        profile=profile,
        passport=passport,
        schema_version=schema_version,
    )
    parser_record_id = _verify_parser_evidence(
        members=members,
        manifest=manifest,
        source=source,
        observation=observation,
        profile=profile,
        graph=graph,
        schema_version=schema_version,
    )
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
        *auxiliary_source_ids,
        *auxiliary_result_ids,
    }
    if parser_record_id is not None:
        evidence_universe.add(parser_record_id)
    raw_auxiliary_results = observation.get("auxiliary_results", [])
    if not isinstance(raw_auxiliary_results, list):
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "auxiliary result inventory is not a list"
        )
    auxiliary_results_by_id = {
        str(item.get("result_id")): item
        for item in raw_auxiliary_results
        if isinstance(item, dict) and isinstance(item.get("result_id"), str)
    }
    for assertion in passport.assertions:
        if assertion.field_name.startswith("auxiliary_result."):
            result_id = assertion.field_name.removeprefix("auxiliary_result.")
            result = auxiliary_results_by_id.get(result_id)
            source_id = None if result is None else result.get("source_artifact_id")
            if (
                result_id not in auxiliary_result_ids
                or not isinstance(source_id, str)
                or assertion.origin != "user_supplied"
                or assertion.transformation != "none"
                or set(assertion.evidence_ids) != {result_id, source_id}
            ):
                raise ExperimentPackageVerificationError(
                    "package_lineage_open",
                    f"auxiliary assertion {assertion.assertion_id} has no retained result record",
                )
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
    if lineage.get("parser_record_id") != parser_record_id:
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "lineage inventory does not match the parser record"
        )
    raw_auxiliary_source_ids = lineage.get("auxiliary_source_artifact_ids", [])
    raw_auxiliary_result_ids = lineage.get("auxiliary_result_ids", [])
    if (
        not isinstance(raw_auxiliary_source_ids, list)
        or set(map(str, raw_auxiliary_source_ids)) != auxiliary_source_ids
    ):
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "lineage inventory does not match auxiliary sources"
        )
    if (
        not isinstance(raw_auxiliary_result_ids, list)
        or set(map(str, raw_auxiliary_result_ids)) != auxiliary_result_ids
    ):
        raise ExperimentPackageVerificationError(
            "package_lineage_open", "lineage inventory does not match auxiliary results"
        )


def verify_experiment_package(
    package_bytes: bytes,
    *,
    object_store: ObjectStore | None = None,
) -> PackageVerification | CampaignPackageVerification:
    """Verify shared closure and producer-specific report, lineage, and object contracts."""
    members = _open_members(package_bytes)
    manifest = _manifest(members)
    _verify_members(members, manifest)
    core = {key: value for key, value in manifest.items() if key != "package_id"}
    expected_package_id = content_id("experiment-package", core)
    if manifest.get("package_id") != expected_package_id:
        raise ExperimentPackageVerificationError(
            "package_identity_mismatch", "package_id does not match the canonical manifest"
        )
    producer_kind = manifest.get("producer_kind", "cv")
    if producer_kind == "campaign":
        campaign_package = importlib.import_module("labbridge.evidence.campaign_package")
        return cast(
            "CampaignPackageVerification",
            campaign_package.verify_campaign_package_members(
                package_bytes,
                members,
                manifest,
                object_store=object_store,
            ),
        )
    if producer_kind != "cv":
        raise ExperimentPackageVerificationError(
            "package_producer_unsupported", "the Package producer is not supported"
        )
    schema_version = manifest.get("schema_version")
    if schema_version not in {
        LEGACY_PACKAGE_SCHEMA_VERSION,
        PACKAGE_SCHEMA_VERSION,
        ELECTROLYSIS_PACKAGE_SCHEMA_VERSION,
    }:
        raise ExperimentPackageVerificationError(
            "package_schema_unsupported", "the Package schema version is not supported"
        )
    try:
        passport = ExperimentPassport.model_validate_json(members["passport/passport.json"])
    except (KeyError, ValueError) as error:
        raise ExperimentPackageVerificationError(
            "package_passport_invalid", "passport/passport.json is invalid"
        ) from error
    if (
        passport.technique == "galvanostatic_electrolysis"
        and schema_version != ELECTROLYSIS_PACKAGE_SCHEMA_VERSION
    ) or (
        passport.technique == "cyclic_voltammetry"
        and schema_version == ELECTROLYSIS_PACKAGE_SCHEMA_VERSION
    ):
        raise ExperimentPackageVerificationError(
            "package_schema_mismatch", "Package schema and Passport technique differ"
        )
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
    _verify_lineage(
        members,
        passport,
        manifest=manifest,
        schema_version=str(schema_version),
    )
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
    "ELECTROLYSIS_PACKAGE_SCHEMA_VERSION",
    "LEGACY_PACKAGE_SCHEMA_VERSION",
    "PACKAGE_SCHEMA_VERSION",
    "AuxiliaryPackageSource",
    "BuiltExperimentPackage",
    "ExperimentPackage",
    "ExperimentPackageVerificationError",
    "PackageInputs",
    "PackageVerification",
    "build_experiment_package",
    "verify_experiment_package",
]

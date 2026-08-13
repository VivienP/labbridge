"""Versioned EchemDB exchange boundary for LabBridge CV evidence."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import Literal, cast, get_args

from frictionless import Package
from jsonschema import Draft7Validator, FormatChecker  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

from labbridge.domain.cv import ColumnRole
from labbridge.domain.cv_observations import NormalisedCVObservation, NormalisedSeries
from labbridge.domain.experiments import Experiment, MetadataAssertion
from labbridge.domain.source_artifacts import SourceArtifact

ADAPTER_VERSION = "echemdb-cv/1"
ECHEMDB_SCHEMA_VERSION = "0.8.3"
ECHEMDB_SCHEMA_COMMIT = "f48f583f83b1de9f5601d05dae5e5fcd1c25a3f0"
DATA_PACKAGE_PROFILE_VERSION = "2.0"
FRICTIONLESS_VERSION = "5.19.0"
JSONSCHEMA_VERSION = "4.26.0"
REFERENCING_VERSION = "0.37.0"

SCHEMA_ROOT = Path(__file__).with_name("schemas")
ECHEMDB_SCHEMA_PATH = SCHEMA_ROOT / "echemdb" / ECHEMDB_SCHEMA_VERSION / "echemdb_package.json"
DATARESOURCE_SCHEMA_PATH = (
    SCHEMA_ROOT / "frictionless" / DATA_PACKAGE_PROFILE_VERSION / "dataresource.json"
)
DATAPACKAGE_SCHEMA_PATH = (
    SCHEMA_ROOT / "frictionless" / DATA_PACKAGE_PROFILE_VERSION / "datapackage.json"
)
SCHEMA_SHA256 = {
    "echemdb_package.json": "8e9f652d129bbce5b38c8c8092a715d185b7ff8eef7d9a1f7273ed128a73c8ce",
    "dataresource.json": "067d769b44b22178615620d37ac4e9f732cf0459c5540255cbba2480aa91c35a",
    "datapackage.json": "a9ef0fc168b3402ae7aa7d22bbcb798e0db6b639e7ee15ff4aa177463cea7112",
}

REQUIRED_ASSERTIONS = (
    "exchange.source.citation_key",
    "exchange.source.url",
    "exchange.system.type",
    "exchange.electrolyte.type",
    "exchange.figure.type",
    "exchange.curation.process",
    "exchange.experimental",
    "exchange.electrodes",
)

_ECHEMDB_FIGURE_TYPES = {"digitized", "raw", "simulated", "processed"}
_ECHEMDB_ELECTROLYTE_TYPES = {"aqueous", "ionic liquid", "non-aqueous", "solid"}


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExportTrace(_Model):
    external_path: str
    source_kind: Literal["assertion", "observation"]
    source_id: str
    qualifier: str


class MappingEntry(_Model):
    labbridge_path: str
    external_path: str | None
    status: Literal["mapped", "companion", "lossy", "omitted", "unmapped"]
    source_kind: Literal["assertion", "observation"] | None = None
    source_id: str | None = None
    assertion_origin: str | None = None
    value_state: str | None = None
    semantic_review: Literal["not_applicable", "contract_checked", "fixture_declaration"] = (
        "not_applicable"
    )
    semantic_review_note: str | None = None
    loss_reason: str | None = None
    note: str


class MappingReport(_Model):
    schema_version: Literal["1"]
    adapter_version: str
    echemdb_schema_version: str
    echemdb_schema_commit: str
    data_package_profile_version: str
    frictionless_version: str
    entries: tuple[MappingEntry, ...]
    untraced_exported_paths: tuple[str, ...]
    mapping_collisions: tuple[str, ...]


class CVExchange(_Model):
    descriptor: dict[str, object]
    csv_bytes: bytes
    report: MappingReport
    traces: tuple[ExportTrace, ...]
    provenance: dict[str, object]


class ExternalValidation(_Model):
    schema_version: Literal["1"]
    valid: bool
    echemdb_schema_valid: bool
    data_package_profile_valid: bool
    frictionless_valid: bool
    versions: dict[str, str]
    schema_sha256: dict[str, str]
    errors: tuple[str, ...]


class RoundTripSeries(_Model):
    series_id: str
    source_column: str
    role: ColumnRole
    unit: str
    values: tuple[Decimal, ...]


class EchemDBExchangeError(ValueError):
    """A deterministic export rejection with one machine-readable cause."""

    def __init__(self, code: str, message: str, *, field_name: str | None = None) -> None:
        self.code = code
        self.field_name = field_name
        super().__init__(message)


def _required_assertions(experiment: Experiment) -> dict[str, MetadataAssertion]:
    active = {assertion.field_name: assertion for assertion in experiment.active_assertions}
    for field_name in REQUIRED_ASSERTIONS:
        assertion = active.get(field_name)
        if assertion is None:
            raise EchemDBExchangeError(
                "echemdb_required_assertion_missing",
                f"required EchemDB field {field_name} has no explicit assertion",
                field_name=field_name,
            )
        if assertion.origin != "user_supplied":
            raise EchemDBExchangeError(
                "echemdb_required_assertion_origin",
                f"required EchemDB field {field_name} must be user_supplied",
                field_name=field_name,
            )
        if assertion.value.state != "known" or not isinstance(assertion.value.value, str):
            raise EchemDBExchangeError(
                "echemdb_required_assertion_unknown",
                f"required EchemDB field {field_name} must be a known string assertion",
                field_name=field_name,
            )
    return active


def _assertion_text(assertions: dict[str, MetadataAssertion], field_name: str) -> str:
    assertion = assertions[field_name]
    value = assertion.value.value
    if assertion.origin != "user_supplied" or assertion.value.state != "known":
        raise EchemDBExchangeError(
            "echemdb_assertion_not_exportable",
            f"EchemDB field {field_name} is not a known user assertion",
            field_name=field_name,
        )
    if not isinstance(value, str) or not value:
        raise EchemDBExchangeError(
            "echemdb_assertion_not_exportable",
            f"EchemDB field {field_name} must contain a non-empty string",
            field_name=field_name,
        )
    return value


def _validate_assertion_values(assertions: dict[str, MetadataAssertion]) -> None:
    if _assertion_text(assertions, "exchange.system.type") != "electrochemical":
        raise EchemDBExchangeError(
            "echemdb_assertion_value_unsupported",
            "exchange.system.type must equal the pinned schema literal electrochemical",
            field_name="exchange.system.type",
        )
    if _assertion_text(assertions, "exchange.electrolyte.type") not in (_ECHEMDB_ELECTROLYTE_TYPES):
        raise EchemDBExchangeError(
            "echemdb_assertion_value_unsupported",
            "exchange.electrolyte.type is not supported by the pinned schema",
            field_name="exchange.electrolyte.type",
        )
    if _assertion_text(assertions, "exchange.figure.type") not in _ECHEMDB_FIGURE_TYPES:
        raise EchemDBExchangeError(
            "echemdb_assertion_value_unsupported",
            "exchange.figure.type is not supported by the pinned schema",
            field_name="exchange.figure.type",
        )
    if not _assertion_text(assertions, "exchange.source.url").startswith(("https://", "http://")):
        raise EchemDBExchangeError(
            "echemdb_assertion_value_unsupported",
            "exchange.source.url must be an explicit HTTP(S) URL",
            field_name="exchange.source.url",
        )
    for field_name in (
        "exchange.curation.process",
        "exchange.experimental",
        "exchange.electrodes",
    ):
        if _assertion_text(assertions, field_name) != "empty":
            raise EchemDBExchangeError(
                "echemdb_assertion_value_unsupported",
                f"{field_name} must explicitly equal empty for adapter version 1",
                field_name=field_name,
            )


def required_mapping_paths(
    experiment: Experiment, observation: NormalisedCVObservation
) -> frozenset[str]:
    """Return the complete internal field inventory the mapping report must classify."""
    paths = {f"experiment.{name}" for name in type(experiment).model_fields}
    paths.update(f"observation.{name}" for name in type(observation).model_fields)
    paths.update(f"observation.metadata.{name}" for name in type(observation.metadata).model_fields)
    paths.update(f"observation.series[].{name}" for name in NormalisedSeries.model_fields)
    return frozenset(paths)


def validate_mapping_entries(
    entries: tuple[MappingEntry, ...] | list[MappingEntry],
    *,
    required_labbridge_paths: frozenset[str] | None = None,
) -> None:
    by_external_path: dict[str, str] = {}
    by_labbridge_path: set[str] = set()
    for entry in entries:
        if entry.labbridge_path in by_labbridge_path:
            raise EchemDBExchangeError(
                "echemdb_mapping_duplicate",
                f"{entry.labbridge_path} is classified more than once",
                field_name=entry.labbridge_path,
            )
        by_labbridge_path.add(entry.labbridge_path)
        if entry.external_path is None:
            continue
        previous = by_external_path.get(entry.external_path)
        if previous is not None:
            raise EchemDBExchangeError(
                "echemdb_mapping_collision",
                f"{entry.external_path} is mapped by both {previous} and {entry.labbridge_path}",
            )
        by_external_path[entry.external_path] = entry.labbridge_path
    if required_labbridge_paths is not None:
        missing = sorted(required_labbridge_paths - by_labbridge_path)
        if missing:
            raise EchemDBExchangeError(
                "echemdb_mapping_incomplete",
                f"mapping report does not classify {missing[0]}",
                field_name=missing[0],
            )


def _csv_bytes(observation: NormalisedCVObservation) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow([series.source_column for series in observation.series])
    for row_index in range(observation.row_count):
        writer.writerow([str(series.values[row_index]) for series in observation.series])
    return stream.getvalue().encode("utf-8")


def _assertion_trace(
    assertion: MetadataAssertion, external_path: str, *, qualifier: str
) -> ExportTrace:
    return ExportTrace(
        external_path=external_path,
        source_kind="assertion",
        source_id=assertion.assertion_id,
        qualifier=qualifier,
    )


def _observation_trace(
    observation: NormalisedCVObservation,
    external_path: str,
    *,
    source_id: str | None = None,
    qualifier: str,
) -> ExportTrace:
    return ExportTrace(
        external_path=external_path,
        source_kind="observation",
        source_id=source_id or observation.observation_id,
        qualifier=qualifier,
    )


def _base_mapping_entries(
    experiment: Experiment, observation: NormalisedCVObservation
) -> dict[str, MappingEntry]:
    note = "Retained in the LabBridge companion manifest; no lossless EchemDB field exists."
    entries = {
        path: MappingEntry(
            labbridge_path=path,
            external_path=None,
            status="companion",
            source_kind="observation",
            source_id=observation.observation_id,
            note=note,
        )
        for path in required_mapping_paths(experiment, observation)
    }
    for field_name in type(observation.metadata).model_fields:
        value = getattr(observation.metadata, field_name)
        path = f"observation.metadata.{field_name}"
        entries[path] = MappingEntry(
            labbridge_path=path,
            external_path=None,
            status="omitted" if value.state != "known" else "unmapped",
            source_kind="observation",
            source_id=observation.observation_id,
            value_state=value.state,
            note=(
                "Unknown or unsupported metadata is not exported."
                if value.state != "known"
                else "Known metadata has no reviewed lossless mapping in adapter version 1."
            ),
        )
    series_mappings = {
        "source_column": ("mapped", "/resources/0/schema/fields/*/name"),
        "unit": ("mapped", "/resources/0/schema/fields/*/unit"),
        "values": ("mapped", "/resources/0/data/rows/*"),
    }
    for name, (status, external_path) in series_mappings.items():
        path = f"observation.series[].{name}"
        entries[path] = entries[path].model_copy(
            update={
                "external_path": external_path,
                "status": status,
                "note": "Copied without numeric conversion or semantic reinterpretation.",
            }
        )
    return entries


def _mapping_entries(
    experiment: Experiment,
    observation: NormalisedCVObservation,
    assertions: dict[str, MetadataAssertion],
) -> tuple[MappingEntry, ...]:
    entries = _base_mapping_entries(experiment, observation)
    external_assertions = {
        "exchange.source.citation_key": "/resources/0/metadata/echemdb/source/citationKey",
        "exchange.source.url": "/resources/0/metadata/echemdb/source/url",
        "exchange.system.type": "/resources/0/metadata/echemdb/system/type",
        "exchange.electrolyte.type": "/resources/0/metadata/echemdb/system/electrolyte/type",
        "exchange.measurement_type": (
            "/resources/0/metadata/echemdb/figureDescription/measurementType"
        ),
        "exchange.curation.process": "/resources/0/metadata/echemdb/curation/process",
        "exchange.experimental": "/resources/0/metadata/echemdb/experimental",
        "exchange.electrodes": "/resources/0/metadata/echemdb/system/electrodes",
    }
    for field_name, external_path in external_assertions.items():
        assertion = assertions.get(field_name)
        if assertion is None:
            continue
        path = f"experiment.assertions[{field_name}]"
        semantic = field_name in {
            "exchange.system.type",
            "exchange.electrolyte.type",
            "exchange.measurement_type",
            "exchange.electrodes",
        }
        entries[path] = MappingEntry(
            labbridge_path=path,
            external_path=external_path,
            status="mapped",
            source_kind="assertion",
            source_id=assertion.assertion_id,
            assertion_origin=assertion.origin,
            value_state=assertion.value.state,
            semantic_review="fixture_declaration" if semantic else "contract_checked",
            semantic_review_note=(
                "Project-owned synthetic fixture declaration; not independently established "
                "as a source or physical-system property."
                if semantic
                else "Checked only for faithful copying and target-schema constraints."
            ),
            note="Copied from an explicit user assertion without semantic coercion.",
        )
    figure_assertion = assertions["exchange.figure.type"]
    lossy_path = "observation.data_origin + observation.execution_mode"
    entries[lossy_path] = MappingEntry(
        labbridge_path=lossy_path,
        external_path="/resources/0/metadata/echemdb/figureDescription/type",
        status="lossy",
        source_kind="assertion",
        source_id=figure_assertion.assertion_id,
        assertion_origin=figure_assertion.origin,
        value_state=figure_assertion.value.state,
        semantic_review="fixture_declaration",
        semantic_review_note=(
            "Project-owned synthetic fixture projection; not a source-declared classification."
        ),
        loss_reason=(
            "EchemDB figureDescription.type cannot represent LabBridge data_origin and "
            "execution_mode as independent dimensions."
        ),
        note=(
            "An explicit user assertion supplies the external projection; the companion "
            "manifest preserves both LabBridge dimensions."
        ),
    )
    result = tuple(entries[path] for path in sorted(entries))
    validate_mapping_entries(
        result, required_labbridge_paths=required_mapping_paths(experiment, observation)
    )
    return result


def _identity_checks(
    experiment: Experiment,
    observation: NormalisedCVObservation,
    source_artifact: SourceArtifact,
) -> None:
    if experiment.observation_id != observation.observation_id:
        raise EchemDBExchangeError(
            "echemdb_evidence_identity_mismatch", "experiment and observation identities differ"
        )
    if experiment.source_artifact_id != source_artifact.source_artifact_id:
        raise EchemDBExchangeError(
            "echemdb_evidence_identity_mismatch", "experiment and source identities differ"
        )
    if observation.source_artifact_id != source_artifact.source_artifact_id:
        raise EchemDBExchangeError(
            "echemdb_evidence_identity_mismatch", "observation and source identities differ"
        )
    if (
        experiment.data_origin != observation.data_origin
        or experiment.execution_mode != observation.execution_mode
        or source_artifact.data_origin != observation.data_origin
        or source_artifact.execution_mode != observation.execution_mode
    ):
        raise EchemDBExchangeError(
            "echemdb_evidence_origin_mismatch", "evidence origin or execution mode differs"
        )


def _exported_paths(value: object, path: str = "") -> set[str]:
    if isinstance(value, dict):
        if not value:
            return {path}
        return {
            item for key, child in value.items() for item in _exported_paths(child, f"{path}/{key}")
        }
    if isinstance(value, list):
        if not value:
            return {path}
        return {
            item
            for index, child in enumerate(value)
            for item in _exported_paths(child, f"{path}/{index}")
        }
    return {path}


def _descriptor(
    assertions: dict[str, MetadataAssertion],
    observation: NormalisedCVObservation,
    source_artifact: SourceArtifact,
) -> dict[str, object]:
    fields = [
        {"name": series.source_column, "type": "number", "unit": series.unit}
        for series in observation.series
    ]
    figure_description: dict[str, object] = {
        "type": _assertion_text(assertions, "exchange.figure.type"),
        "fields": fields,
    }
    if "exchange.measurement_type" in assertions:
        figure_description["measurementType"] = _assertion_text(
            assertions, "exchange.measurement_type"
        )
    return {
        "$schema": "https://datapackage.org/profiles/2.0/datapackage.json",
        "profile": "data-package",
        "name": f"labbridge-{observation.observation_id.split(':', 1)[-1]}",
        "resources": [
            {
                "name": "cv",
                "type": "table",
                "path": "cv.csv",
                "scheme": "file",
                "format": "csv",
                "mediatype": "text/csv",
                "encoding": "utf-8",
                "schema": {"fields": fields},
                "metadata": {
                    "echemdb": {
                        "echemdbSchemaVersion": ECHEMDB_SCHEMA_VERSION,
                        "curation": {"process": []},
                        "experimental": {},
                        "figureDescription": figure_description,
                        "source": {
                            "citationKey": _assertion_text(
                                assertions, "exchange.source.citation_key"
                            ),
                            "url": _assertion_text(assertions, "exchange.source.url"),
                            "originalFilename": source_artifact.filename,
                        },
                        "system": {
                            "type": _assertion_text(assertions, "exchange.system.type"),
                            "electrolyte": {
                                "type": _assertion_text(assertions, "exchange.electrolyte.type")
                            },
                            "electrodes": [],
                        },
                    }
                },
            }
        ],
    }


def _traces(
    experiment: Experiment,
    observation: NormalisedCVObservation,
    descriptor: dict[str, object],
    assertions: dict[str, MetadataAssertion],
) -> tuple[ExportTrace, ...]:
    by_path: dict[str, ExportTrace] = {}
    assertion_paths = {
        "exchange.source.citation_key": "/resources/0/metadata/echemdb/source/citationKey",
        "exchange.source.url": "/resources/0/metadata/echemdb/source/url",
        "exchange.system.type": "/resources/0/metadata/echemdb/system/type",
        "exchange.electrolyte.type": "/resources/0/metadata/echemdb/system/electrolyte/type",
        "exchange.figure.type": "/resources/0/metadata/echemdb/figureDescription/type",
        "exchange.measurement_type": (
            "/resources/0/metadata/echemdb/figureDescription/measurementType"
        ),
        "exchange.curation.process": "/resources/0/metadata/echemdb/curation/process",
        "exchange.experimental": "/resources/0/metadata/echemdb/experimental",
        "exchange.electrodes": "/resources/0/metadata/echemdb/system/electrodes",
    }
    for field_name, path in assertion_paths.items():
        if field_name in assertions:
            by_path[path] = _assertion_trace(
                assertions[field_name],
                path,
                qualifier="Explicit user-supplied external metadata; not source-declared.",
            )
    source_assertion = next(
        assertion
        for assertion in experiment.active_assertions
        if assertion.field_name == "source_artifact"
    )
    source_path = "/resources/0/metadata/echemdb/source/originalFilename"
    by_path[source_path] = _assertion_trace(
        source_assertion,
        source_path,
        qualifier="Resolved through the asserted retained source artifact identity.",
    )
    for index, series in enumerate(observation.series):
        for parent in (
            "/resources/0/schema/fields",
            "/resources/0/metadata/echemdb/figureDescription/fields",
        ):
            for key in ("name", "type", "unit"):
                path = f"{parent}/{index}/{key}"
                by_path[path] = _observation_trace(
                    observation,
                    path,
                    source_id=series.series_id,
                    qualifier="Retained normalised series metadata.",
                )
        header_path = f"/resources/0/data/header/{series.source_column}"
        by_path[header_path] = _observation_trace(
            observation,
            header_path,
            source_id=series.series_id,
            qualifier="Retained normalised series source column.",
        )
        for row_index in range(observation.row_count):
            path = f"/resources/0/data/rows/{row_index}/{series.source_column}"
            by_path[path] = _observation_trace(
                observation,
                path,
                source_id=series.series_id,
                qualifier="Retained normalised observation value.",
            )
    for path in _exported_paths(descriptor):
        by_path.setdefault(
            path,
            _observation_trace(
                observation,
                path,
                qualifier="Deterministic adapter structure for this observation.",
            ),
        )
    return tuple(by_path[path] for path in sorted(by_path))


def build_cv_exchange(
    *,
    experiment: Experiment,
    observation: NormalisedCVObservation,
    source_artifact: SourceArtifact,
) -> CVExchange:
    """Map one CV observation without importing EchemDB concepts into the domain model."""
    _identity_checks(experiment, observation, source_artifact)
    assertions = _required_assertions(experiment)
    _validate_assertion_values(assertions)
    if "exchange.measurement_type" in assertions:
        _assertion_text(assertions, "exchange.measurement_type")
    descriptor = _descriptor(assertions, observation, source_artifact)
    traces = _traces(experiment, observation, descriptor, assertions)
    exported_paths = _exported_paths(descriptor)
    exported_paths.update(
        f"/resources/0/data/header/{series.source_column}" for series in observation.series
    )
    exported_paths.update(
        f"/resources/0/data/rows/{row_index}/{series.source_column}"
        for row_index in range(observation.row_count)
        for series in observation.series
    )
    traced_paths = {trace.external_path for trace in traces}
    untraced = tuple(sorted(exported_paths - traced_paths))
    entries = _mapping_entries(experiment, observation, assertions)
    report = MappingReport(
        schema_version="1",
        adapter_version=ADAPTER_VERSION,
        echemdb_schema_version=ECHEMDB_SCHEMA_VERSION,
        echemdb_schema_commit=ECHEMDB_SCHEMA_COMMIT,
        data_package_profile_version=DATA_PACKAGE_PROFILE_VERSION,
        frictionless_version=FRICTIONLESS_VERSION,
        entries=entries,
        untraced_exported_paths=untraced,
        mapping_collisions=(),
    )
    provenance: dict[str, object] = {
        "schema_version": "1",
        "adapter_version": ADAPTER_VERSION,
        "data_origin": observation.data_origin,
        "execution_mode": observation.execution_mode,
        "environment_id": observation.environment_id,
        "experiment": experiment.model_dump(mode="json"),
        "observation_id": observation.observation_id,
        "source_artifact": source_artifact.model_dump(mode="json"),
        "import_profile_id": observation.import_profile_id,
        "parser_record_id": observation.parser_record_id,
        "transformation_ids": observation.transformation_ids,
        "series": [
            {
                "series_id": series.series_id,
                "source_column": series.source_column,
                "role": series.role,
                "unit": series.unit,
                "source_unit": series.source_unit,
                "transformation_id": series.transformation_id,
            }
            for series in observation.series
        ],
        "traces": [trace.model_dump(mode="json") for trace in traces],
    }
    return CVExchange(
        descriptor=descriptor,
        csv_bytes=_csv_bytes(observation),
        report=report,
        traces=traces,
        provenance=provenance,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_exchange(exchange: CVExchange) -> ExternalValidation:
    """Validate an exchange against only the exact vendored and installed versions."""
    versions = {
        "echemdb_metadata_schema": ECHEMDB_SCHEMA_VERSION,
        "data_package_profile": DATA_PACKAGE_PROFILE_VERSION,
        "frictionless": version("frictionless"),
        "jsonschema": version("jsonschema"),
        "referencing": version("referencing"),
    }
    expected_versions = {
        "echemdb_metadata_schema": ECHEMDB_SCHEMA_VERSION,
        "data_package_profile": DATA_PACKAGE_PROFILE_VERSION,
        "frictionless": FRICTIONLESS_VERSION,
        "jsonschema": JSONSCHEMA_VERSION,
        "referencing": REFERENCING_VERSION,
    }
    errors = [
        f"version mismatch for {name}: expected {expected}, found {versions[name]}"
        for name, expected in expected_versions.items()
        if versions[name] != expected
    ]
    actual_hashes = {
        "echemdb_package.json": _sha256(ECHEMDB_SCHEMA_PATH),
        "dataresource.json": _sha256(DATARESOURCE_SCHEMA_PATH),
        "datapackage.json": _sha256(DATAPACKAGE_SCHEMA_PATH),
    }
    errors.extend(
        f"schema checksum mismatch for {name}: expected {expected}, found {actual_hashes[name]}"
        for name, expected in SCHEMA_SHA256.items()
        if actual_hashes[name] != expected
    )
    echemdb_schema = json.loads(ECHEMDB_SCHEMA_PATH.read_text(encoding="utf-8"))
    dataresource_schema = json.loads(DATARESOURCE_SCHEMA_PATH.read_text(encoding="utf-8"))
    datapackage_schema = json.loads(DATAPACKAGE_SCHEMA_PATH.read_text(encoding="utf-8"))
    registry = Registry().with_resource(
        "https://datapackage.org/profiles/2.0/dataresource.json",
        Resource.from_contents(dataresource_schema, default_specification=DRAFT7),
    )
    validator = Draft7Validator(
        echemdb_schema,
        registry=registry,
        format_checker=FormatChecker(),
    )
    schema_errors = tuple(
        f"{item.json_path}: {item.message}"
        for item in sorted(
            validator.iter_errors(exchange.descriptor), key=lambda item: item.json_path
        )
    )
    errors.extend(f"EchemDB schema: {item}" for item in schema_errors)
    data_package_validator = Draft7Validator(
        datapackage_schema,
        format_checker=FormatChecker(),
    )
    data_package_errors = tuple(
        f"{item.json_path}: {item.message}"
        for item in sorted(
            data_package_validator.iter_errors(exchange.descriptor),
            key=lambda item: item.json_path,
        )
    )
    errors.extend(f"Data Package profile: {item}" for item in data_package_errors)
    with tempfile.TemporaryDirectory(prefix="labbridge-echemdb-") as raw_directory:
        directory = Path(raw_directory)
        descriptor_path = directory / "datapackage.json"
        descriptor_path.write_text(
            json.dumps(exchange.descriptor, sort_keys=True, indent=2), encoding="utf-8"
        )
        (directory / "cv.csv").write_bytes(exchange.csv_bytes)
        frictionless_report = Package(descriptor_path).validate()
    frictionless_errors: tuple[str, ...] = ()
    if not frictionless_report.valid:
        frictionless_errors = (
            json.dumps(
                frictionless_report.to_descriptor(),
                sort_keys=True,
                default=str,
            ),
        )
        errors.extend(f"Frictionless: {item}" for item in frictionless_errors)
    if exchange.report.untraced_exported_paths:
        errors.extend(
            f"untraced exported path: {path}" for path in exchange.report.untraced_exported_paths
        )
    return ExternalValidation(
        schema_version="1",
        valid=not errors,
        echemdb_schema_valid=not schema_errors,
        data_package_profile_valid=not data_package_errors,
        frictionless_valid=not frictionless_errors,
        versions=versions,
        schema_sha256=actual_hashes,
        errors=tuple(errors),
    )


def round_trip_series(exchange: CVExchange) -> tuple[RoundTripSeries, ...]:
    """Restore only the series semantics that the exchange package preserves exactly."""
    reader = csv.DictReader(io.StringIO(exchange.csv_bytes.decode("utf-8")))
    rows = list(reader)
    raw_series = exchange.provenance.get("series")
    if not isinstance(raw_series, list):
        raise EchemDBExchangeError(
            "echemdb_companion_invalid", "companion manifest has no series inventory"
        )
    restored: list[RoundTripSeries] = []
    allowed_roles = set(get_args(ColumnRole))
    for raw in raw_series:
        if not isinstance(raw, dict):
            raise EchemDBExchangeError(
                "echemdb_companion_invalid", "companion series entry is not an object"
            )
        source_column = str(raw["source_column"])
        raw_role = str(raw["role"])
        if raw_role not in allowed_roles:
            raise EchemDBExchangeError(
                "echemdb_companion_invalid", f"companion series role {raw_role} is unsupported"
            )
        restored.append(
            RoundTripSeries(
                series_id=str(raw["series_id"]),
                source_column=source_column,
                role=cast(ColumnRole, raw_role),
                unit=str(raw["unit"]),
                values=tuple(Decimal(row[source_column]) for row in rows),
            )
        )
    return tuple(restored)


__all__ = [
    "ADAPTER_VERSION",
    "DATA_PACKAGE_PROFILE_VERSION",
    "ECHEMDB_SCHEMA_COMMIT",
    "ECHEMDB_SCHEMA_VERSION",
    "FRICTIONLESS_VERSION",
    "JSONSCHEMA_VERSION",
    "REFERENCING_VERSION",
    "CVExchange",
    "EchemDBExchangeError",
    "ExternalValidation",
    "MappingEntry",
    "build_cv_exchange",
    "required_mapping_paths",
    "round_trip_series",
    "validate_exchange",
    "validate_mapping_entries",
]

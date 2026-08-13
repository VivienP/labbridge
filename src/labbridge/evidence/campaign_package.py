"""Campaign producer for the closed Experiment Package integrity envelope."""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Connection, select

from labbridge.domain.canonical import content_id
from labbridge.domain.events import BudgetEntryPayload, ObservationRelationPayload
from labbridge.domain.identity import EnvironmentRef
from labbridge.domain.provenance import Provenance
from labbridge.domain.results import (
    AttemptOutcome,
    DerivedMetric,
    Observation,
    metric_id,
    observation_id,
)
from labbridge.infrastructure.objectstore import (
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
)
from labbridge.infrastructure.persistence.tables import (
    attempt_outcomes,
    attempts,
    budget_ledger,
    campaigns,
    record_relations,
    work_items,
)
from labbridge.runtime.events import read_stream, validate_exported_stream
from labbridge.runtime.replay import CampaignReplay, reconstruct_campaign

from .bundle import _metric_rows, _object_rows, _observation_rows
from .experiment_package import (
    MANIFEST_MEMBER,
    ExperimentPackageVerificationError,
    _member_entry,
    _zip,
)
from .manifest import canonical_json, digest

CAMPAIGN_PACKAGE_SCHEMA_VERSION = "campaign/1"
CAMPAIGN_PRODUCER_KIND = "campaign"
CAMPAIGN_EVENT_STREAM_CONTRACT_VERSION = 2

_JSON_MEMBERS = {
    "campaign/declaration.json": "declaration",
    "campaign/environment.json": "environment",
    "campaign/attempts-outcomes.json": "attempts_outcomes",
    "campaign/observations.json": "observations",
    "campaign/derived-metrics.json": "derived_metrics",
    "campaign/relations.json": "relations",
    "campaign/budget-ledger.json": "budget_ledger",
    "campaign/failures-recoveries.json": "failures_recoveries",
    "campaign/source-inventory.json": "source_inventory",
    "campaign/object-inventory.json": "object_inventory",
    "campaign/raw-results.json": "raw_results",
    "campaign/limitations.json": "limitations",
    "campaign/producing-versions.json": "producing_versions",
}
_REQUIRED_MEMBERS = frozenset({*_JSON_MEMBERS, "campaign/events.jsonl", "campaign/report.html"})


class CampaignPackageInputs(BaseModel):
    """Complete immutable campaign evidence used to build one package."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_id: str = Field(min_length=1)
    declaration: dict[str, object]
    environment: dict[str, object]
    events: list[dict[str, object]]
    attempts_outcomes: list[dict[str, object]]
    observations: list[dict[str, object]]
    derived_metrics: list[dict[str, object]]
    relations: list[dict[str, object]]
    budget_ledger: list[dict[str, object]]
    failures_recoveries: list[dict[str, object]]
    source_inventory: list[dict[str, object]]
    object_inventory: list[dict[str, object]]
    raw_results: list[dict[str, object]]
    report_html: str = Field(min_length=1)
    limitations: list[str]
    producing_versions: dict[str, str]
    supersedes_package_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _inputs_are_canonical_json_values(cls, value: object) -> object:
        def normalise(item: object) -> object:  # noqa: PLR0911
            if item is None or isinstance(item, (str, bool, int)):
                return item
            if isinstance(item, float):
                if not math.isfinite(item):
                    raise ValueError("campaign package inputs must be JSON-compatible")
                return item
            if isinstance(item, Decimal):
                return str(item)
            if isinstance(item, uuid.UUID):
                return str(item)
            if isinstance(item, datetime):
                if item.tzinfo is None or item.utcoffset() is None:
                    raise ValueError("campaign package inputs must use aware datetimes")
                return item.isoformat()
            if isinstance(item, Mapping):
                if any(not isinstance(key, str) for key in item):
                    raise ValueError("campaign package inputs must use JSON-compatible keys")
                return {str(key): normalise(child) for key, child in item.items()}
            if isinstance(item, (list, tuple)):
                return [normalise(child) for child in item]
            raise ValueError("campaign package inputs must be JSON-compatible")

        return normalise(value)


class CampaignExperimentPackage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    package_id: str
    schema_version: Literal["campaign/1"]
    producer_kind: Literal["campaign"]
    campaign_id: str
    data_origin: Literal["observed", "synthetic"]
    execution_mode: Literal["replay", "simulation", "live"]
    environment_id: str
    archive_sha256: str
    archive_byte_size: int
    producing_versions: dict[str, str]


@dataclass(frozen=True)
class BuiltCampaignExperimentPackage:
    metadata: CampaignExperimentPackage
    archive_bytes: bytes

    @property
    def package_id(self) -> str:
        return self.metadata.package_id


class CampaignPackageVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verified: Literal[True]
    producer_kind: Literal["campaign"]
    package_id: str
    campaign_id: str
    data_origin: Literal["observed", "synthetic"]
    execution_mode: Literal["replay", "simulation", "live"]
    environment_id: str
    archive_sha256: str
    lineage_closed: Literal[True]
    objects_referenced: int
    objects_verified: int
    verification_scope: Literal["package", "full"]


def _events_bytes(events: list[dict[str, object]]) -> bytes:
    if not events:
        return b""
    lines = [
        json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        for event in events
    ]
    return b"\n".join(lines) + b"\n"


def build_campaign_experiment_package(
    inputs: CampaignPackageInputs,
) -> BuiltCampaignExperimentPackage:
    """Build a deterministic campaign archive without mutating operational state."""
    environment = EnvironmentRef.model_validate(inputs.environment)
    members = {
        name: canonical_json(getattr(inputs, field_name))
        for name, field_name in _JSON_MEMBERS.items()
    }
    members["campaign/events.jsonl"] = _events_bytes(inputs.events)
    members["campaign/report.html"] = inputs.report_html.encode("utf-8")
    entries = [_member_entry(name, data) for name, data in sorted(members.items())]
    core: dict[str, object] = {
        "artifact_kind": "experiment_package",
        "schema_version": CAMPAIGN_PACKAGE_SCHEMA_VERSION,
        "producer_kind": CAMPAIGN_PRODUCER_KIND,
        "campaign_id": inputs.campaign_id,
        "data_origin": environment.data_origin,
        "execution_mode": environment.execution_mode,
        "environment_id": environment.environment_id,
        "supersedes_package_id": inputs.supersedes_package_id,
        "producing_versions": dict(sorted(inputs.producing_versions.items())),
        "members": entries,
        "members_digest": digest(canonical_json(entries)),
    }
    package_id = content_id("experiment-package", core)
    members[MANIFEST_MEMBER] = canonical_json({**core, "package_id": package_id})
    archive_bytes = _zip(members)
    metadata = CampaignExperimentPackage(
        package_id=package_id,
        schema_version=CAMPAIGN_PACKAGE_SCHEMA_VERSION,
        producer_kind=CAMPAIGN_PRODUCER_KIND,
        campaign_id=inputs.campaign_id,
        data_origin=environment.data_origin,
        execution_mode=environment.execution_mode,
        environment_id=environment.environment_id,
        archive_sha256=digest(archive_bytes),
        archive_byte_size=len(archive_bytes),
        producing_versions=dict(sorted(inputs.producing_versions.items())),
    )
    return BuiltCampaignExperimentPackage(metadata=metadata, archive_bytes=archive_bytes)


def _attempt_outcome_rows(
    connection: Connection, campaign_id: uuid.UUID
) -> list[dict[str, object]]:
    rows = connection.execute(
        select(
            attempts.c.attempt_id,
            attempts.c.work_item_id,
            attempts.c.job_id,
            attempts.c.ordinal,
            attempts.c.state.label("attempt_state"),
            attempts.c.started_at,
            attempts.c.created_at,
            attempt_outcomes.c.campaign_id,
            attempt_outcomes.c.status,
            attempt_outcomes.c.observation_id,
            attempt_outcomes.c.failure,
            attempt_outcomes.c.cost,
            attempt_outcomes.c.data_origin,
            attempt_outcomes.c.execution_mode,
            attempt_outcomes.c.provenance,
            attempt_outcomes.c.finished_at,
        )
        .select_from(
            attempts.join(
                work_items, attempts.c.work_item_id == work_items.c.work_item_id
            ).outerjoin(attempt_outcomes, attempts.c.attempt_id == attempt_outcomes.c.attempt_id)
        )
        .where(work_items.c.campaign_id == campaign_id)
        .order_by(attempts.c.work_item_id, attempts.c.ordinal, attempts.c.attempt_id)
    ).mappings()
    result: list[dict[str, object]] = []
    for row in rows:
        if row["status"] is None:
            raise ValueError("a campaign package cannot release an attempt without an outcome")
        result.append(
            {
                "attempt_id": str(row["attempt_id"]),
                "work_item_id": str(row["work_item_id"]),
                "campaign_id": str(row["campaign_id"]),
                "job_id": str(row["job_id"]) if row["job_id"] is not None else None,
                "ordinal": row["ordinal"],
                "attempt_state": row["attempt_state"],
                "status": row["status"],
                "observation_id": row["observation_id"],
                "failure": row["failure"],
                "cost": row["cost"],
                "data_origin": row["data_origin"],
                "execution_mode": row["execution_mode"],
                "provenance": row["provenance"],
                "started_at": row["started_at"],
                "created_at": row["created_at"],
                "finished_at": row["finished_at"],
            }
        )
    return result


def _relation_rows(
    connection: Connection,
    observations_payload: list[dict[str, object]],
    metrics_payload: list[dict[str, object]],
) -> list[dict[str, object]]:
    identities = {str(row["observation_id"]) for row in observations_payload} | {
        str(row["metric_id"]) for row in metrics_payload
    }
    if not identities:
        return []
    rows = connection.execute(
        select(record_relations)
        .where(
            record_relations.c.subject_id.in_(identities),
            record_relations.c.object_id.in_(identities),
        )
        .order_by(record_relations.c.recorded_at, record_relations.c.relation_id)
    ).mappings()
    relations = [
        {
            "relation_id": str(row["relation_id"]),
            "subject_id": row["subject_id"],
            "predicate": row["predicate"],
            "object_id": row["object_id"],
            "reason": row["reason"],
            "recorded_at": row["recorded_at"],
        }
        for row in rows
    ]
    edges = {
        (str(row["subject_id"]), str(row["predicate"]), str(row["object_id"])) for row in relations
    }
    for metric in metrics_payload:
        edge = (str(metric["metric_id"]), "derived_from", str(metric["observation_id"]))
        if edge in edges:
            continue
        relation_body = {
            "subject_id": edge[0],
            "predicate": edge[1],
            "object_id": edge[2],
        }
        relations.append(
            {
                "relation_id": content_id("record-relation", relation_body),
                **relation_body,
                "reason": "metric derived from observation",
                "recorded_at": metric["created_at"],
            }
        )
    return sorted(relations, key=lambda row: (str(row["recorded_at"]), str(row["relation_id"])))


def _budget_rows(connection: Connection, campaign_id: uuid.UUID) -> list[dict[str, object]]:
    rows = connection.execute(
        select(budget_ledger)
        .where(budget_ledger.c.campaign_id == campaign_id)
        .order_by(budget_ledger.c.recorded_at, budget_ledger.c.entry_id)
    ).mappings()
    return [
        {key: str(value) if isinstance(value, uuid.UUID) else value for key, value in row.items()}
        for row in rows
    ]


def _root_inventory(observations_payload: list[dict[str, object]]) -> list[dict[str, object]]:
    by_digest: dict[str, dict[str, object]] = {}
    for observation in observations_payload:
        provenance = Provenance.model_validate(observation["provenance"])
        root = provenance.source_record or provenance.synthetic_root
        if root is None:
            raise ValueError("an exported observation has no lineage root")
        body = root.model_dump(mode="json")
        by_digest[digest(canonical_json(body))] = body
    return [by_digest[key] for key in sorted(by_digest)]


def _failure_recovery_rows(
    attempts_payload: list[dict[str, object]], events_payload: list[dict[str, object]]
) -> list[dict[str, object]]:
    failures = [
        {
            "kind": "failure",
            "attempt_id": row["attempt_id"],
            "status": row["status"],
            "failure": row.get("failure"),
        }
        for row in attempts_payload
        if row.get("failure") is not None
        or row["status"] in {"corrupted", "lease_lost", "timed_out"}
    ]

    def is_recovery_event(row: dict[str, object]) -> bool:
        event_type = str(row.get("event_type", ""))
        payload = row.get("payload")
        return event_type == "job.lease_expired" or (
            event_type == "job.available"
            and isinstance(payload, dict)
            and payload.get("last_failure") is not None
        )

    recoveries = [
        {
            "kind": "recovery",
            "event_id": row.get("event_id"),
            "campaign_position": row.get("campaign_position"),
            "event_type": row.get("event_type"),
            "payload": row.get("payload"),
        }
        for row in events_payload
        if is_recovery_event(row)
    ]
    return [*failures, *recoveries]


def _raw_results(
    attempts_payload: list[dict[str, object]],
    observations_payload: list[dict[str, object]],
    metrics_payload: list[dict[str, object]],
) -> list[dict[str, object]]:
    observation_by_attempt = {str(row["attempt_id"]): row for row in observations_payload}
    metrics_by_attempt: dict[str, list[dict[str, object]]] = {}
    for metric in metrics_payload:
        metrics_by_attempt.setdefault(str(metric["attempt_id"]), []).append(metric)
    return [
        {
            "attempt_id": row["attempt_id"],
            "outcome_status": row["status"],
            "data_origin": row["data_origin"],
            "execution_mode": row["execution_mode"],
            "observation": observation_by_attempt.get(str(row["attempt_id"])),
            "metrics": metrics_by_attempt.get(str(row["attempt_id"]), []),
        }
        for row in attempts_payload
    ]


def _render_report(
    *,
    campaign_id: uuid.UUID,
    campaign_name: str,
    environment: EnvironmentRef,
    attempts_payload: list[dict[str, object]],
    observations_payload: list[dict[str, object]],
    metrics_payload: list[dict[str, object]],
    limitations: list[str],
) -> str:
    label = f"{environment.data_origin} + {environment.execution_mode}"
    synthetic = (
        "<p><strong>Synthetic</strong> campaign evidence.</p>" if environment.is_synthetic else ""
    )
    limitations_html = "".join(f"<li>{escape(item)}</li>" for item in limitations)
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>Campaign evidence</title>'
        "</head><body>"
        f"<h1>{escape(campaign_name)}</h1><p>Campaign: {campaign_id}</p>"
        f"<p>Origin and execution mode: {label}</p>{synthetic}"
        f"<p>Attempts: {len(attempts_payload)}; observations: {len(observations_payload)}; "
        f"derived metrics: {len(metrics_payload)}.</p>"
        f"<h2>Limitations</h2><ul>{limitations_html}</ul></body></html>"
    )


def campaign_package_inputs_from_postgres(
    connection: Connection,
    campaign_id: uuid.UUID,
    *,
    producing_versions: dict[str, str],
    limitations: list[str],
) -> CampaignPackageInputs:
    """Load one complete v2 campaign snapshot from authoritative PostgreSQL tables."""
    campaign = (
        connection.execute(select(campaigns).where(campaigns.c.campaign_id == campaign_id))
        .mappings()
        .one()
    )
    if campaign["event_stream_contract_version"] != CAMPAIGN_EVENT_STREAM_CONTRACT_VERSION:
        raise ValueError("campaign Experiment Packages require event stream contract version 2")
    environment = EnvironmentRef(
        environment_id=campaign["environment_id"],
        adapter_version=campaign["adapter_version"],
        data_origin=campaign["data_origin"],
        execution_mode=campaign["execution_mode"],
    )
    events_payload = read_stream(connection, campaign_id)
    attempts_payload = _attempt_outcome_rows(connection, campaign_id)
    observations_payload = _observation_rows(connection, campaign_id)
    metrics_payload = _metric_rows(connection, campaign_id)
    object_inventory = list(_object_rows(connection, campaign_id))
    return CampaignPackageInputs(
        campaign_id=str(campaign_id),
        declaration=dict(campaign["declaration"]),
        environment=environment.model_dump(mode="json"),
        events=events_payload,
        attempts_outcomes=attempts_payload,
        observations=observations_payload,
        derived_metrics=metrics_payload,
        relations=_relation_rows(connection, observations_payload, metrics_payload),
        budget_ledger=_budget_rows(connection, campaign_id),
        failures_recoveries=_failure_recovery_rows(attempts_payload, events_payload),
        source_inventory=_root_inventory(observations_payload),
        object_inventory=object_inventory,
        raw_results=_raw_results(attempts_payload, observations_payload, metrics_payload),
        report_html=_render_report(
            campaign_id=campaign_id,
            campaign_name=str(campaign["name"]),
            environment=environment,
            attempts_payload=attempts_payload,
            observations_payload=observations_payload,
            metrics_payload=metrics_payload,
            limitations=limitations,
        ),
        limitations=limitations,
        producing_versions=producing_versions,
    )


def _error(code: str, message: str) -> ExperimentPackageVerificationError:
    return ExperimentPackageVerificationError(code, message)


def _load_json(members: dict[str, bytes], name: str) -> object:
    try:
        return json.loads(members[name])
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _error("package_member_invalid", f"{name} is missing or invalid JSON") from error


def _list_member(members: dict[str, bytes], name: str) -> list[object]:
    value = _load_json(members, name)
    if not isinstance(value, list):
        raise _error("package_member_invalid", f"{name} must contain a JSON array")
    return value


def _verify_identity(manifest: dict[str, object], members: dict[str, bytes]) -> EnvironmentRef:
    if set(_REQUIRED_MEMBERS) - set(members):
        missing = sorted(set(_REQUIRED_MEMBERS) - set(members))[0]
        raise _error("package_schema_mismatch", f"campaign package omits {missing}")
    environment_body = _load_json(members, "campaign/environment.json")
    try:
        environment = EnvironmentRef.model_validate(environment_body)
    except ValueError as error:
        raise _error("package_origin_mismatch", "campaign environment is invalid") from error
    expected = (
        manifest.get("environment_id"),
        manifest.get("data_origin"),
        manifest.get("execution_mode"),
    )
    actual = (
        environment.environment_id,
        environment.data_origin,
        environment.execution_mode,
    )
    if actual != expected:
        raise _error("package_origin_mismatch", "manifest and campaign environment differ")
    versions = _load_json(members, "campaign/producing-versions.json")
    if versions != manifest.get("producing_versions") or not isinstance(versions, dict):
        raise _error("package_schema_mismatch", "producing versions differ from the manifest")
    if versions.get("campaign_package") != "1":
        raise _error("package_schema_mismatch", "campaign_package producing version is missing")
    return environment


def _verify_events(members: dict[str, bytes], *, campaign_id: uuid.UUID) -> CampaignReplay:
    raw = members["campaign/events.jsonl"]
    try:
        events = [json.loads(line) for line in raw.splitlines()]
    except json.JSONDecodeError as error:
        raise _error("package_member_invalid", "events.jsonl is invalid") from error
    try:
        stream = validate_exported_stream(events, campaign_id=campaign_id)
        return reconstruct_campaign(stream, contract_version=CAMPAIGN_EVENT_STREAM_CONTRACT_VERSION)
    except (ValueError, RuntimeError) as error:
        raise _error(
            "package_event_stream_invalid", "campaign event stream is semantically invalid"
        ) from error


def _verify_lineage(  # noqa: PLR0912, PLR0915 - each branch closes one evidence seam
    manifest: dict[str, object], members: dict[str, bytes], environment: EnvironmentRef
) -> list[dict[str, object]]:
    observations = _list_member(members, "campaign/observations.json")
    metrics = _list_member(members, "campaign/derived-metrics.json")
    outcomes = _list_member(members, "campaign/attempts-outcomes.json")
    roots = _list_member(members, "campaign/source-inventory.json")
    object_inventory = _list_member(members, "campaign/object-inventory.json")
    referenced_roots: dict[str, dict[str, object]] = {}
    observation_keys: dict[tuple[str, str], dict[str, object]] = {}
    for value in observations:
        if not isinstance(value, dict):
            raise _error("package_lineage_open", "observation inventory row is invalid")
        try:
            provenance = Provenance.model_validate(value.get("provenance"))
        except ValueError as error:
            raise _error("package_lineage_open", "observation provenance is invalid") from error
        if not provenance.has_root:
            raise _error("package_lineage_open", "an observation has no lineage root")
        if (
            value.get("data_origin") != environment.data_origin
            or value.get("execution_mode") != environment.execution_mode
            or provenance.environment != environment
        ):
            raise _error("package_origin_mismatch", "observation origin or mode differs")
        root = provenance.source_record or provenance.synthetic_root
        assert root is not None
        root_body = root.model_dump(mode="json")
        root_digest = digest(canonical_json(root_body))
        referenced_roots[root_digest] = root_body
        if root_body not in roots:
            raise _error("package_lineage_open", "observation root is absent from inventory")
        key = (str(value.get("observation_id", "")), str(value.get("attempt_id", "")))
        if not all(key) or key in observation_keys:
            raise _error("package_lineage_open", "observation receipt identity is invalid")
        observation_keys[key] = value
    for value in outcomes:
        if not isinstance(value, dict):
            raise _error("package_lineage_open", "attempt outcome row is invalid")
        if (
            value.get("data_origin") != environment.data_origin
            or value.get("execution_mode") != environment.execution_mode
        ):
            raise _error("package_origin_mismatch", "attempt outcome origin or mode differs")
        try:
            outcome_provenance = Provenance.model_validate(value.get("provenance"))
        except ValueError as error:
            raise _error("package_lineage_open", "attempt outcome provenance is invalid") from error
        if outcome_provenance.environment != environment:
            raise _error("package_origin_mismatch", "attempt outcome environment differs")
        observation_id = value.get("observation_id")
        if (
            observation_id is not None
            and (str(observation_id), str(value.get("attempt_id", ""))) not in observation_keys
        ):
            raise _error("package_lineage_open", "outcome names an absent observation receipt")
    outcome_receipts = {
        (str(value.get("observation_id")), str(value.get("attempt_id", "")))
        for value in outcomes
        if isinstance(value, dict) and value.get("observation_id") is not None
    }
    if outcome_receipts != set(observation_keys):
        raise _error("package_lineage_open", "observation receipts and attempt outcomes differ")
    for value in metrics:
        if not isinstance(value, dict):
            raise _error("package_lineage_open", "derived metric row is invalid")
        key = (str(value.get("observation_id", "")), str(value.get("attempt_id", "")))
        observation = observation_keys.get(key)
        if observation is None:
            raise _error("package_lineage_open", "metric names an absent observation receipt")
        try:
            provenance = Provenance.model_validate(value.get("provenance"))
        except ValueError as error:
            raise _error("package_lineage_open", "metric provenance is invalid") from error
        if provenance.environment != environment or not provenance.has_root:
            raise _error("package_origin_mismatch", "metric origin or lineage root differs")
        observation_provenance = Provenance.model_validate(observation["provenance"])
        if (
            provenance.source_record != observation_provenance.source_record
            or provenance.synthetic_root != observation_provenance.synthetic_root
        ):
            raise _error("package_lineage_open", "metric and observation roots differ")
    inventory_roots: dict[str, dict[str, object]] = {}
    for value in roots:
        if not isinstance(value, dict):
            raise _error("package_lineage_open", "source inventory row is invalid")
        root_digest = digest(canonical_json(value))
        if root_digest in inventory_roots:
            raise _error("package_lineage_open", "source inventory repeats a root")
        inventory_roots[root_digest] = value
    if inventory_roots != referenced_roots:
        raise _error("package_lineage_open", "source inventory contains missing or unused roots")
    inventory_by_receipt: dict[tuple[str, str], dict[str, object]] = {}
    for entry in object_inventory:
        if not isinstance(entry, dict):
            raise _error("package_lineage_open", "object inventory row is invalid")
        key = (str(entry.get("observation_id", "")), str(entry.get("attempt_id", "")))
        if key in inventory_by_receipt:
            raise _error("package_lineage_open", "object inventory repeats a receipt")
        inventory_by_receipt[key] = entry
    if set(inventory_by_receipt) != set(observation_keys):
        raise _error("package_lineage_open", "object inventory and receipts differ")
    for key, observation in observation_keys.items():
        entry = inventory_by_receipt[key]
        if (
            any(
                entry.get(field) != observation.get(field)
                for field in ("sha256", "byte_size", "media_type", "object_uri")
            )
            or entry.get("lifecycle_state") != "committed"
        ):
            raise _error("package_lineage_open", "object metadata and receipt differ")
    del manifest
    return list(inventory_by_receipt.values())


def _verify_outcomes(
    members: dict[str, bytes], replay: CampaignReplay
) -> dict[str, dict[str, object]]:
    outcomes = _list_member(members, "campaign/attempts-outcomes.json")
    outcome_by_attempt: dict[str, dict[str, object]] = {}
    for value in outcomes:
        if not isinstance(value, dict):
            raise _error("package_projection_mismatch", "attempt outcome row is invalid")
        attempt_id = str(value.get("attempt_id", ""))
        if not attempt_id or attempt_id in outcome_by_attempt:
            raise _error("package_projection_mismatch", "attempt outcomes are not unique")
        outcome_by_attempt[attempt_id] = value
    replay_attempts = {
        str(attempt.attempt_id): attempt
        for attempt in replay.attempts
        if attempt.outcome is not None
    }
    if set(outcome_by_attempt) != set(replay_attempts):
        raise _error("package_projection_mismatch", "attempt outcome inventory differs from replay")
    for attempt_id, row in outcome_by_attempt.items():
        attempt = replay_attempts[attempt_id]
        outcome = attempt.outcome
        assert outcome is not None
        try:
            packaged_outcome = AttemptOutcome.model_validate(
                {
                    key: row[key]
                    for key in (
                        "attempt_id",
                        "work_item_id",
                        "status",
                        "observation_id",
                        "failure",
                        "cost",
                        "started_at",
                        "finished_at",
                        "provenance",
                    )
                }
            )
        except ValueError as error:
            raise _error("package_projection_mismatch", "attempt outcome is invalid") from error
        canonical_outcome = packaged_outcome.model_dump(mode="json")
        comparable_row = {
            **row,
            "failure": canonical_outcome["failure"],
            "cost": canonical_outcome["cost"],
            "provenance": canonical_outcome["provenance"],
        }
        expected = {
            "attempt_id": attempt_id,
            "work_item_id": str(attempt.work_item_id),
            "campaign_id": str(outcome.campaign_id),
            "job_id": str(attempt.job_id) if attempt.job_id is not None else None,
            "ordinal": attempt.ordinal,
            "attempt_state": attempt.state,
            "status": outcome.status,
            "observation_id": outcome.observation_id,
            "failure": outcome.failure,
            "cost": outcome.cost,
            "data_origin": outcome.data_origin,
            "execution_mode": outcome.execution_mode,
            "provenance": outcome.provenance,
            "started_at": outcome.started_at.isoformat() if outcome.started_at else None,
            "created_at": attempt.created_at.isoformat(),
            "finished_at": outcome.finished_at.isoformat(),
        }
        if comparable_row != expected:
            differing_fields = sorted(
                key
                for key in set(comparable_row) | set(expected)
                if comparable_row.get(key) != expected.get(key)
            )
            raise _error(
                "package_projection_mismatch",
                f"attempt {attempt_id} differs from replay in {', '.join(differing_fields)}",
            )
    return outcome_by_attempt


def _verify_observations(
    members: dict[str, bytes], replay: CampaignReplay
) -> list[dict[str, object]]:
    observations = _list_member(members, "campaign/observations.json")
    observation_keys = {
        (str(row.observation_id), str(row.attempt_id)): row for row in replay.observations
    }
    packaged_keys: set[tuple[str, str]] = set()
    for value in observations:
        if not isinstance(value, dict):
            raise _error("package_projection_mismatch", "observation row is invalid")
        key = (str(value.get("observation_id", "")), str(value.get("attempt_id", "")))
        if key in packaged_keys or key not in observation_keys:
            raise _error("package_projection_mismatch", "observation inventory differs from replay")
        packaged_keys.add(key)
        replayed = observation_keys[key]
        expected = {
            "observation_id": replayed.observation_id,
            "campaign_id": str(replayed.campaign_id),
            "attempt_id": str(replayed.attempt_id),
            "work_item_id": str(replayed.work_item_id),
            "sha256": replayed.sha256,
            "byte_size": replayed.byte_size,
            "object_uri": replayed.object_uri,
            "media_type": replayed.media_type,
            "schema_version": replayed.schema_version,
            "signal_kind": replayed.signal_kind,
            "quantities": list(replayed.quantities),
            "status": replayed.status,
            "status_reason": replayed.status_reason,
            "data_origin": replayed.data_origin,
            "execution_mode": replayed.execution_mode,
            "provenance": replayed.provenance,
            "received_at": replayed.received_at.isoformat(),
        }
        if value != expected:
            raise _error("package_projection_mismatch", f"observation {key[0]} differs from replay")
        try:
            observation = Observation.model_validate(
                {
                    key: item
                    for key, item in value.items()
                    if key not in {"data_origin", "execution_mode"}
                }
            )
        except ValueError as error:
            raise _error("package_projection_mismatch", "observation row is invalid") from error
        expected_identity = observation_id(
            sha256=observation.sha256,
            schema_version=observation.schema_version,
            signal_kind=observation.signal_kind,
            quantities=observation.quantities,
            provenance=observation.provenance,
        )
        if observation.observation_id != expected_identity:
            raise _error("package_lineage_open", "observation identity is not canonical")
    if packaged_keys != set(observation_keys):
        raise _error("package_projection_mismatch", "observation inventory omits replay receipts")
    return [row for row in observations if isinstance(row, dict)]


def _verify_metrics_and_raw_results(
    members: dict[str, bytes],
    environment: EnvironmentRef,
    outcomes: dict[str, dict[str, object]],
    observations: list[dict[str, object]],
) -> None:
    metrics = _list_member(members, "campaign/derived-metrics.json")
    metrics_by_attempt: dict[str, list[dict[str, object]]] = {}
    metric_ids: set[str] = set()
    for value in metrics:
        if not isinstance(value, dict):
            raise _error("package_projection_mismatch", "metric row is invalid")
        try:
            metric = DerivedMetric.model_validate(
                {
                    key: value.get(key)
                    for key in (
                        "metric_id",
                        "observation_id",
                        "name",
                        "value",
                        "uncertainty",
                        "analysis_name",
                        "analysis_version",
                        "parameter_hash",
                        "quality_status",
                        "quality_reason",
                        "provenance",
                    )
                }
            )
        except ValueError as error:
            raise _error("package_lineage_open", "derived metric is invalid") from error
        attempt_id = str(value.get("attempt_id", ""))
        expected_identity = metric_id(
            observation_id=metric.observation_id,
            attempt_id=attempt_id,
            name=metric.name,
            analysis_name=metric.analysis_name,
            analysis_version=metric.analysis_version,
            parameter_hash=metric.parameter_hash,
        )
        if (
            not attempt_id
            or metric.metric_id != expected_identity
            or metric.metric_id in metric_ids
            or value.get("unit") != metric.value.unit
        ):
            raise _error("package_lineage_open", "derived metric identity is invalid")
        metric_ids.add(metric.metric_id)
        provenance = metric.provenance
        if (
            value.get("environment_id") != environment.environment_id
            or value.get("data_origin") != environment.data_origin
            or value.get("execution_mode") != environment.execution_mode
            or provenance.environment != environment
        ):
            raise _error("package_projection_mismatch", "metric origin fields differ")
        metrics_by_attempt.setdefault(attempt_id, []).append(value)

    raw_results = _list_member(members, "campaign/raw-results.json")
    expected_raw = [
        {
            "attempt_id": attempt_id,
            "outcome_status": row["status"],
            "data_origin": row["data_origin"],
            "execution_mode": row["execution_mode"],
            "observation": next(
                (
                    item
                    for item in observations
                    if isinstance(item, dict) and str(item.get("attempt_id")) == attempt_id
                ),
                None,
            ),
            "metrics": metrics_by_attempt.get(attempt_id, []),
        }
        for attempt_id, row in outcomes.items()
    ]
    if raw_results != expected_raw:
        raise _error("package_projection_mismatch", "raw results differ from per-attempt evidence")


def _verify_budget_entries(ledger: list[object], event_rows: list[dict[str, object]]) -> None:
    expected_ledger: list[dict[str, object]] = []
    for event in event_rows:
        if not str(event.get("event_type", "")).startswith("budget."):
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise _error("package_event_stream_invalid", "budget event payload is invalid")
        expected_ledger.append({"campaign_id": str(event["campaign_id"]), **payload})
    try:
        canonical_ledger = [
            {
                "campaign_id": str(row["campaign_id"]),
                **BudgetEntryPayload.model_validate(
                    {key: value for key, value in row.items() if key != "campaign_id"}
                ).model_dump(mode="json"),
            }
            for row in ledger
            if isinstance(row, dict)
        ]
        canonical_expected = [
            {
                "campaign_id": str(row["campaign_id"]),
                **BudgetEntryPayload.model_validate(
                    {key: value for key, value in row.items() if key != "campaign_id"}
                ).model_dump(mode="json"),
            }
            for row in expected_ledger
        ]
    except (KeyError, TypeError, ValueError) as error:
        raise _error("package_projection_mismatch", "budget ledger row is invalid") from error
    if len(canonical_ledger) != len(ledger):
        raise _error("package_projection_mismatch", "budget ledger row is invalid")
    canonical_ledger.sort(key=lambda row: (str(row["recorded_at"]), str(row["entry_id"])))
    canonical_expected.sort(key=lambda row: (str(row["recorded_at"]), str(row["entry_id"])))
    if canonical_ledger != canonical_expected:
        raise _error("package_projection_mismatch", "budget ledger differs from v2 events")


def _verify_budget(
    members: dict[str, bytes], replay: CampaignReplay, event_rows: list[dict[str, object]]
) -> None:
    ledger = _list_member(members, "campaign/budget-ledger.json")
    _verify_budget_entries(ledger, event_rows)
    kinds = ("reserved", "consumed", "released", "adjusted_up", "adjusted_down")
    totals = {kind: Decimal(0) for kind in kinds}
    seen_entries: set[str] = set()
    settled_reservations: set[str] = set()
    reservations: dict[str, Decimal] = {}
    for value in ledger:
        if not isinstance(value, dict):
            raise _error("package_projection_mismatch", "budget ledger row is invalid")
        entry_id = str(value.get("entry_id", ""))
        kind = str(value.get("kind", ""))
        if not entry_id or entry_id in seen_entries or kind not in totals:
            raise _error("package_projection_mismatch", "budget ledger identity is invalid")
        seen_entries.add(entry_id)
        try:
            amount = Decimal(str(value.get("amount")))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise _error("package_projection_mismatch", "budget amount is invalid") from error
        if amount <= 0:
            raise _error("package_projection_mismatch", "budget amount must be positive")
        totals[kind] += amount
        if kind == "reserved":
            reservations[entry_id] = amount
        elif value.get("reservation_entry_id") is not None:
            settled_reservations.add(str(value["reservation_entry_id"]))
    outstanding = sum(
        (
            amount
            for entry_id, amount in reservations.items()
            if entry_id not in settled_reservations
        ),
        Decimal(0),
    )
    consumed = totals["consumed"] + totals["adjusted_up"] - totals["adjusted_down"]
    if (
        totals["reserved"] != replay.budget.reserved
        or totals["released"] != replay.budget.released
        or totals["adjusted_up"] != replay.budget.adjusted_up
        or totals["adjusted_down"] != replay.budget.adjusted_down
        or consumed != replay.budget.consumed
        or outstanding != replay.budget.outstanding
    ):
        raise _error("package_projection_mismatch", "budget ledger differs from replay")


def _verify_projection(
    members: dict[str, bytes], replay: CampaignReplay, environment: EnvironmentRef
) -> list[dict[str, object]]:
    declaration = _load_json(members, "campaign/declaration.json")
    if declaration != replay.campaign.declaration:
        raise _error("package_projection_mismatch", "declaration differs from campaign.created")
    if (
        replay.campaign.environment_id != environment.environment_id
        or replay.campaign.adapter_version != environment.adapter_version
        or replay.campaign.data_origin != environment.data_origin
        or replay.campaign.execution_mode != environment.execution_mode
    ):
        raise _error("package_projection_mismatch", "environment differs from campaign.created")
    outcomes = _verify_outcomes(members, replay)
    observations = _verify_observations(members, replay)
    _verify_metrics_and_raw_results(members, environment, outcomes, observations)
    event_rows = [json.loads(line) for line in members["campaign/events.jsonl"].splitlines()]
    expected_failures = _failure_recovery_rows(list(outcomes.values()), event_rows)
    if _load_json(members, "campaign/failures-recoveries.json") != expected_failures:
        raise _error(
            "package_projection_mismatch", "failure and recovery summary differs from records"
        )
    _verify_budget(members, replay, event_rows)
    return event_rows


def _verify_relations(members: dict[str, bytes], event_rows: list[dict[str, object]]) -> None:
    observations = _list_member(members, "campaign/observations.json")
    metrics = _list_member(members, "campaign/derived-metrics.json")
    universe = {str(row.get("observation_id")) for row in observations if isinstance(row, dict)} | {
        str(row.get("metric_id")) for row in metrics if isinstance(row, dict)
    }
    known_predicates = {"derived_from", "supersedes", "invalidates"}
    seen: set[tuple[str, str, str]] = set()
    seen_ids: set[str] = set()
    relations = _list_member(members, "campaign/relations.json")
    rows: list[dict[str, object]] = []
    for value in relations:
        if not isinstance(value, dict):
            raise _error("package_lineage_open", "relation row is invalid")
        subject = str(value.get("subject_id", ""))
        predicate = str(value.get("predicate", ""))
        object_id = str(value.get("object_id", ""))
        edge = (subject, predicate, object_id)
        relation_id = str(value.get("relation_id", ""))
        if (
            subject == object_id
            or predicate not in known_predicates
            or subject not in universe
            or object_id not in universe
            or edge in seen
            or not relation_id
            or relation_id in seen_ids
            or not value.get("reason")
            or not value.get("recorded_at")
        ):
            raise _error("package_lineage_open", "relation graph is open or invalid")
        seen.add(edge)
        seen_ids.add(relation_id)
        rows.append(value)
    expected_metric_edges = {
        (str(row.get("metric_id")), "derived_from", str(row.get("observation_id")))
        for row in metrics
        if isinstance(row, dict)
    }
    actual_metric_edges = {edge for edge in seen if edge[1] == "derived_from"}
    if actual_metric_edges != expected_metric_edges:
        raise _error("package_lineage_open", "metric relation inventory differs from metrics")
    expected_event_rows = [
        event["payload"]
        for event in event_rows
        if event.get("event_type") in {"observation.invalidated", "observation.superseded"}
    ]
    actual_event_rows = [row for row in rows if row.get("predicate") != "derived_from"]
    try:
        canonical_actual_events = {
            canonical_json(ObservationRelationPayload.model_validate(row).model_dump(mode="json"))
            for row in actual_event_rows
        }
        canonical_expected_events = {
            canonical_json(ObservationRelationPayload.model_validate(row).model_dump(mode="json"))
            for row in expected_event_rows
        }
    except ValueError as error:
        raise _error("package_lineage_open", "event relation row is invalid") from error
    if canonical_actual_events != canonical_expected_events:
        raise _error("package_lineage_open", "event relation inventory differs from v2 events")


def _verify_report(
    members: dict[str, bytes], environment: EnvironmentRef, *, campaign_id: uuid.UUID
) -> None:
    try:
        report = members["campaign/report.html"].decode("utf-8")
    except UnicodeDecodeError as error:
        raise _error("package_report_mismatch", "campaign report is not UTF-8") from error
    label = f"{environment.data_origin} + {environment.execution_mode}"
    if label not in report:
        raise _error("package_report_mismatch", "campaign report omits origin and mode")
    limitations = _list_member(members, "campaign/limitations.json")
    if not limitations or any(not isinstance(item, str) or not item for item in limitations):
        raise _error("package_report_mismatch", "campaign limitations are absent or invalid")
    for limitation in limitations:
        if not isinstance(limitation, str) or limitation not in report:
            raise _error("package_report_mismatch", "campaign report omits a declared limitation")
    if environment.data_origin == "synthetic" and "Synthetic" not in report:
        raise _error("package_report_mismatch", "synthetic campaign report lacks its visible label")
    expected_tokens = (
        str(campaign_id),
        f"Attempts: {len(_list_member(members, 'campaign/attempts-outcomes.json'))}",
        f"observations: {len(_list_member(members, 'campaign/observations.json'))}",
        f"derived metrics: {len(_list_member(members, 'campaign/derived-metrics.json'))}",
    )
    if any(token not in report for token in expected_tokens):
        raise _error("package_report_mismatch", "campaign report counts or identity differ")


def _verify_objects(inventory: list[dict[str, object]], store: ObjectStore) -> int:
    verified: set[tuple[str, str]] = set()
    for entry in inventory:
        bucket = str(entry.get("bucket", ""))
        key = str(entry.get("key", ""))
        if bucket != store.bucket or entry.get("object_uri") != f"s3://{bucket}/{key}":
            raise _error("object_metadata_inconsistent", "object location is invalid")
        physical = (bucket, key)
        if physical in verified:
            continue
        try:
            data = store.get(key)
        except ObjectNotFoundError as error:
            raise _error("object_missing", f"recorded object {key} is missing") from error
        except ObjectStoreError as error:
            raise _error(
                "object_store_failure", f"recorded object {key} is inaccessible"
            ) from error
        if len(data) != entry.get("byte_size"):
            raise _error("object_size_mismatch", f"recorded object {key} has another size")
        if digest(data) != entry.get("sha256"):
            raise _error("object_sha256_mismatch", f"recorded object {key} has another SHA-256")
        verified.add(physical)
    return len(verified)


def verify_campaign_package_members(
    package_bytes: bytes,
    members: dict[str, bytes],
    manifest: dict[str, object],
    *,
    object_store: ObjectStore | None = None,
) -> CampaignPackageVerification:
    """Validate campaign-specific members after the shared ZIP envelope has closed."""
    if manifest.get("schema_version") != CAMPAIGN_PACKAGE_SCHEMA_VERSION:
        raise _error("package_schema_unsupported", "campaign package schema is unsupported")
    environment = _verify_identity(manifest, members)
    try:
        campaign_id = uuid.UUID(str(manifest["campaign_id"]))
    except (KeyError, ValueError) as error:
        raise _error("package_schema_mismatch", "campaign identity is invalid") from error
    replay = _verify_events(members, campaign_id=campaign_id)
    event_rows = _verify_projection(members, replay, environment)
    _verify_relations(members, event_rows)
    inventory = _verify_lineage(manifest, members, environment)
    _verify_report(members, environment, campaign_id=campaign_id)
    objects_verified = _verify_objects(inventory, object_store) if object_store else 0
    return CampaignPackageVerification(
        verified=True,
        producer_kind="campaign",
        package_id=str(manifest["package_id"]),
        campaign_id=str(manifest["campaign_id"]),
        data_origin=environment.data_origin,
        execution_mode=environment.execution_mode,
        environment_id=environment.environment_id,
        archive_sha256=digest(package_bytes),
        lineage_closed=True,
        objects_referenced=len(inventory),
        objects_verified=objects_verified,
        verification_scope="full" if object_store else "package",
    )


__all__ = [
    "CAMPAIGN_PACKAGE_SCHEMA_VERSION",
    "BuiltCampaignExperimentPackage",
    "CampaignExperimentPackage",
    "CampaignPackageInputs",
    "CampaignPackageVerification",
    "build_campaign_experiment_package",
    "campaign_package_inputs_from_postgres",
    "verify_campaign_package_members",
]

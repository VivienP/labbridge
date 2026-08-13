"""Deterministic machine-readable and self-contained Experiment Passport reports."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from labbridge.domain.canonical import content_id
from labbridge.domain.experiments import (
    Experiment,
    MetadataAssertion,
    ReleaseDecision,
    ValidationFinding,
    ValidationRun,
)
from labbridge.domain.identity import DataOrigin, ExecutionMode

from .manifest import canonical_json

PASSPORT_SCHEMA_VERSION = "1"


class ExperimentPassport(BaseModel):
    """An immutable validation snapshot for one exact experiment version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passport_id: str = Field(min_length=1)
    schema_version: Literal["1"]
    experiment_id: str = Field(min_length=1)
    experiment_version: int = Field(ge=1)
    technique: Literal["cyclic_voltammetry", "galvanostatic_electrolysis"]
    data_origin: DataOrigin
    execution_mode: ExecutionMode
    environment_id: str = Field(min_length=1)
    source_artifact_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    import_profile_id: str = Field(min_length=1)
    transformation_ids: tuple[str, ...] = Field(min_length=1)
    assertions: tuple[MetadataAssertion, ...]
    active_assertion_ids: tuple[str, ...]
    validation_id: str = Field(min_length=1)
    validation_version: str = Field(min_length=1)
    findings: tuple[ValidationFinding, ...]
    release_decision: ReleaseDecision
    release_status: Literal["preview", "released"]
    released_at: datetime | None = None
    supersedes_passport_id: str | None = None

    @model_validator(mode="after")
    def _release_state_is_consistent(self) -> Self:
        if self.release_status == "released" and self.released_at is None:
            raise ValueError("a released Passport requires released_at")
        if self.release_status == "preview" and self.released_at is not None:
            raise ValueError("a Passport preview cannot carry released_at")
        if tuple(item.finding_id for item in self.findings) != self.release_decision.finding_ids:
            raise ValueError("Passport findings and release decision identifiers differ")
        return self


def _passport_body(
    experiment: Experiment,
    validation: ValidationRun,
    *,
    release_status: Literal["preview", "released"],
    supersedes_passport_id: str | None,
) -> dict[str, object]:
    return {
        "schema_version": PASSPORT_SCHEMA_VERSION,
        "experiment_id": experiment.experiment_id,
        "experiment_version": experiment.version,
        "technique": experiment.technique,
        "data_origin": experiment.data_origin,
        "execution_mode": experiment.execution_mode,
        "environment_id": experiment.environment_id,
        "source_artifact_id": experiment.source_artifact_id,
        "observation_id": experiment.observation_id,
        "import_profile_id": experiment.import_profile_id,
        "transformation_ids": experiment.transformation_ids,
        "assertions": experiment.assertions,
        "active_assertion_ids": experiment.active_assertion_ids,
        "validation_id": validation.validation_id,
        "validation_version": validation.validation_version,
        "findings": validation.findings,
        "release_decision": validation.release_decision,
        "release_status": release_status,
        "supersedes_passport_id": supersedes_passport_id,
    }


def build_passport(
    experiment: Experiment,
    validation: ValidationRun,
    *,
    released_at: datetime | None,
    release: bool,
    supersedes_passport_id: str | None = None,
) -> ExperimentPassport:
    """Freeze one validation run; blockers are allowed in previews but not releases."""
    if validation.experiment_id != experiment.experiment_id:
        raise ValueError("validation belongs to another experiment")
    if validation.experiment_version != experiment.version:
        raise ValueError("validation belongs to another experiment version")
    if release and validation.release_decision.status == "blocked":
        raise ValueError("blocking validation findings prevent release")
    if release != (released_at is not None):
        raise ValueError("released_at is present exactly when a Passport is released")
    status: Literal["preview", "released"] = "released" if release else "preview"
    body = _passport_body(
        experiment,
        validation,
        release_status=status,
        supersedes_passport_id=supersedes_passport_id,
    )
    return ExperimentPassport(
        passport_id=content_id("passport", body),
        released_at=released_at,
        **body,
    )


def stable_passport_payload(passport: ExperimentPassport) -> dict[str, object]:
    """Return the deterministic report body with declared volatile release time removed."""
    payload = passport.model_dump(mode="json")
    payload.pop("released_at")
    return payload


def render_passport_json(passport: ExperimentPassport) -> bytes:
    """Render the complete machine-readable Passport deterministically."""
    return canonical_json(passport.model_dump(mode="json"))


def _value(assertion: MetadataAssertion) -> str:
    value = assertion.value
    if value.state != "known":
        return value.state
    rendered = str(value.value)
    return f"{rendered} {value.unit}" if value.unit else rendered


def render_passport_html(passport: ExperimentPassport) -> bytes:
    """Render a standalone HTML report from the same frozen Passport model as JSON."""
    active = set(passport.active_assertion_ids)
    assertion_rows = "".join(
        "<tr>"
        f"<td>{html.escape(item.field_name)}</td>"
        f"<td>{html.escape(_value(item))}</td>"
        f"<td>{html.escape(item.origin)}</td>"
        f"<td>{html.escape(item.transformation)}</td>"
        f"<td>{html.escape(item.requirement_class)}</td>"
        f"<td><code>{html.escape(item.assertion_id)}</code></td>"
        "</tr>"
        for item in passport.assertions
        if item.assertion_id in active
    )
    finding_rows = "".join(
        "<li>"
        f"<strong>{html.escape(item.severity)}</strong> "
        f"<code>{html.escape(item.finding_id)}</code> — "
        f"{html.escape(item.message)} "
        f"Resolution: {html.escape(item.resolution)}"
        "</li>"
        for item in passport.findings
    )
    if not finding_rows:
        finding_rows = "<li>No blocking, warning, or unknown finding.</li>"
    title = f"Experiment Passport {passport.passport_id}"
    origin = html.escape(passport.data_origin)
    execution_mode = html.escape(passport.execution_mode)
    decision_status = html.escape(passport.release_decision.status)
    technique_scope = ""
    if passport.technique == "galvanostatic_electrolysis":
        chemical = next(
            (
                item.value.state
                for item in passport.assertions
                if item.assertion_id in active and item.field_name == "chemical_analysis"
            ),
            "unavailable",
        )
        technique_scope = (
            "\n<h2>Electrical and chemical scope</h2>"
            "<p>Recorded electrical time series: time, current, and potential with explicit units. "
            f"Chemical/product quantification: {html.escape(chemical)}. This Passport does not "
            "report conversion, selectivity, yield, or Faradaic efficiency.</p>"
        )
    report = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;
padding:0 1rem;color:#202124}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #bbb;padding:.45rem;text-align:left}}
code{{overflow-wrap:anywhere}}.decision{{padding:.75rem;border:2px solid #555}}
</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
<p><strong>Origin and mode:</strong> {origin} + {execution_mode}</p>
<p><strong>Technique:</strong> {html.escape(passport.technique)}</p>
<p><strong>Experiment version:</strong> {passport.experiment_version}</p>
<p class="decision"><strong>Release decision: {decision_status}</strong><br>
Blocking: {passport.release_decision.blocking_count};
warnings: {passport.release_decision.warning_count};
unknowns: {passport.release_decision.unknown_count}</p>
<h2>Metadata assertions</h2>
<table><thead><tr><th>Field</th><th>Value state</th><th>Origin</th><th>Transformation</th>
<th>Requirement</th><th>Assertion</th></tr></thead><tbody>{assertion_rows}</tbody></table>
<h2>Validation findings</h2><ul>{finding_rows}</ul>{technique_scope}
<h2>Lineage anchors</h2>
<p>Source artifact: <code>{html.escape(passport.source_artifact_id)}</code><br>
Normalised observation: <code>{html.escape(passport.observation_id)}</code><br>
Import profile: <code>{html.escape(passport.import_profile_id)}</code></p>
<h2>Limitations</h2>
<p>This Passport records evidence completeness and declared metadata. It does not claim scientific
validity, data quality, reproducibility, or fitness for a journal.</p>
</body>
</html>
"""
    return report.encode("utf-8")


__all__ = [
    "PASSPORT_SCHEMA_VERSION",
    "ExperimentPassport",
    "build_passport",
    "render_passport_html",
    "render_passport_json",
    "stable_passport_payload",
]

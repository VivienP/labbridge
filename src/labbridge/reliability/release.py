"""Closed, self-verifying release artifact for recorded fault-campaign results."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from labbridge.evidence.manifest import canonical_json, digest, verify_manifest
from labbridge.reliability.fault_campaign import FaultCampaignResult, summarize_results
from labbridge.reliability.runner import CampaignExecution

_FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
            default=str,
        ).encode("utf-8")
        + b"\n"
    )


def _json_line(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        ).encode("utf-8")
        + b"\n"
    )


def _row_json(row: FaultCampaignResult) -> dict[str, object]:
    return row.model_dump(mode="json")


def _csv_bytes(rows: Sequence[FaultCampaignResult]) -> bytes:
    serialised = [_row_json(row) for row in rows]
    if not serialised:
        raise ValueError("a fault-campaign release requires recorded result rows")
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(serialised[0]))
    writer.writeheader()
    for row in serialised:
        writer.writerow(
            {
                key: json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value
                for key, value in row.items()
            }
        )
    return stream.getvalue().encode("utf-8")


def _packages_zip(executions: Sequence[CampaignExecution]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for execution in sorted(executions, key=lambda item: item.result.ordinal):
            info = zipfile.ZipInfo(execution.package_path.name, _FIXED_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, execution.package_path.read_bytes())
    return stream.getvalue()


def _report_html(report: Mapping[str, object]) -> bytes:
    summary = report["summary"]
    assert isinstance(summary, dict)
    rows = "".join(f"<tr><th>{key}</th><td>{value}</td></tr>" for key, value in summary.items())
    origin = report["data_classification"]
    limitations = report["limitations"]
    if not isinstance(limitations, list):
        raise TypeError("report limitations must be a list")
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        "<title>LabBridge Phase 7 fault campaign</title>"
        "<h1>Phase 7 process-boundary fault campaign</h1>"
        f"<p><strong>Data classification:</strong> {origin}</p>"
        "<p>This report describes at-least-once delivery with idempotent effect handling. "
        "It makes no exactly-once execution claim.</p>"
        f"<table>{rows}</table>"
        "<h2>Scope and exclusions</h2><ul>"
        + "".join(f"<li>{item}</li>" for item in limitations)
        + "</ul></html>\n"
    ).encode("utf-8")


def write_fault_campaign_release(
    destination: Path,
    executions: Sequence[CampaignExecution],
    *,
    environment: Mapping[str, object],
    backup_restore: Mapping[str, object],
    migration_checks: Mapping[str, object],
    reproduce_command: str,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    """Write one flat closed artifact whose claims are derived from retained rows."""
    if not executions:
        raise ValueError("a fault-campaign release requires at least one execution")
    generated = generated_at or datetime.now(UTC)
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    rows = [execution.result for execution in executions]
    classifications = {(row.data_origin, row.execution_mode) for row in rows}
    if len(classifications) != 1:
        raise ValueError("a fault-campaign release cannot mix data origin or execution mode")
    data_origin, execution_mode = classifications.pop()
    if (
        environment.get("data_origin") != data_origin
        or environment.get("execution_mode") != execution_mode
    ):
        raise ValueError("environment manifest origin/mode differs from the result rows")
    summary = summarize_results(rows)
    limitations = [
        f"The released run used {data_origin} bytes in {execution_mode} mode.",
        "Future observed live execution remains outside Phase 7 and was not run.",
        "Results cover only the recorded environment, seeds, and injected boundaries.",
    ]
    report: dict[str, object] = {
        "schema_version": "1",
        "generated_at": generated.isoformat(),
        "run_id": rows[0].run_id,
        "data_classification": f"{data_origin} + {execution_mode}",
        "summary": summary.model_dump(mode="json"),
        "limitations": limitations,
        "backup_restore": dict(backup_restore),
        "migration_checks": dict(migration_checks),
    }
    files: dict[str, bytes] = {
        "fault-campaign-report.json": _json_bytes(report),
        "fault-campaign-report.html": _report_html(report),
        "raw-results.csv": _csv_bytes(rows),
        "summary.json": _json_bytes(summary.model_dump(mode="json")),
        "environment-manifest.json": _json_bytes(dict(environment)),
        "scenario-matrix.json": _json_bytes(summary.fault_point_counts),
        "audit-log.jsonl": b"".join(
            _json_line(execution.audit)
            for execution in sorted(executions, key=lambda item: item.result.ordinal)
        ),
        "backup-restore.json": _json_bytes(dict(backup_restore)),
        "migration-checks.json": _json_bytes(dict(migration_checks)),
        "package-verifications.json": _json_bytes(
            [
                {
                    "campaign_id": str(row.campaign_id),
                    "package_id": row.package_id,
                    "archive_sha256": row.package_sha256,
                    "verified": row.package_verified,
                    "objects_referenced": row.objects_referenced,
                    "objects_verified": row.objects_verified,
                }
                for row in rows
            ]
        ),
        "packages.zip": _packages_zip(executions),
        "LIMITATIONS.md": (
            "# Limitations\n\n" + "\n".join(f"- {x}" for x in limitations) + "\n"
        ).encode(),
        "REPRODUCE.txt": (reproduce_command.rstrip() + "\n").encode(),
    }
    destination.mkdir(parents=True, exist_ok=True)
    for name, payload in files.items():
        (destination / name).write_bytes(payload)
    entries = [
        {
            "name": name,
            "byte_size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for name, payload in sorted(files.items())
    ]
    backup_passed = bool(backup_restore.get("passed"))
    migration_passed = bool(migration_checks.get("passed"))
    manifest: dict[str, object] = {
        "artifact_kind": "phase7_fault_campaign",
        "schema_version": "1",
        "capability_status": (
            "demonstrated"
            if summary.acceptance_met and backup_passed and migration_passed
            else "implemented"
        ),
        "run_id": rows[0].run_id,
        "campaigns_executed": len(rows),
        "data_origin": data_origin,
        "execution_mode": execution_mode,
        "files": entries,
        "files_digest": digest(canonical_json(entries)),
        "summary": summary.model_dump(mode="json"),
    }
    (destination / "manifest.json").write_bytes(_json_bytes(manifest))
    return manifest


def verify_fault_campaign_release(destination: Path) -> dict[str, object]:
    """Verify the closed flat release before any result is cited."""
    manifest = verify_manifest(destination)
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("fault-campaign artifact has no file inventory")
    names: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ValueError("fault-campaign artifact has an invalid file inventory")
        names.add(entry["name"])
    expected = {"manifest.json", *names}
    actual = {path.name for path in destination.iterdir() if path.is_file()}
    if actual != expected:
        raise ValueError("fault-campaign artifact is not a closed file set")
    raw_rows = list(csv.DictReader(io.StringIO((destination / "raw-results.csv").read_text())))
    if not raw_rows:
        raise ValueError("fault-campaign artifact has no raw result rows")
    classifications = {(row.get("data_origin"), row.get("execution_mode")) for row in raw_rows}
    manifest_origin = manifest.get("data_origin")
    manifest_mode = manifest.get("execution_mode")
    if not isinstance(manifest_origin, str) or not isinstance(manifest_mode, str):
        raise ValueError("release manifest has no valid origin/mode classification")
    expected_classification = (manifest_origin, manifest_mode)
    report = json.loads((destination / "fault-campaign-report.json").read_text())
    environment = json.loads((destination / "environment-manifest.json").read_text())
    if classifications != {expected_classification}:
        raise ValueError("raw result origin/mode differs from the release manifest")
    if report.get("data_classification") != " + ".join(expected_classification):
        raise ValueError("report origin/mode differs from the release manifest")
    if (
        environment.get("data_origin"),
        environment.get("execution_mode"),
    ) != expected_classification:
        raise ValueError("environment origin/mode differs from the release manifest")
    return dict(manifest)


__all__ = ["verify_fault_campaign_release", "write_fault_campaign_release"]

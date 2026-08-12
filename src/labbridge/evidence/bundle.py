"""Evidence bundles: an immutable, checksummed export of what a campaign actually recorded.

`docs/SPEC.md` §12 and §4.3. A released bundle is never mutated — a correction is a new bundle with
an explicit relation to the one it supersedes (ADR-006, F-037).

The manifest is the point. Every file carries its SHA-256, and the manifest itself carries a digest
over those entries, so a bundle that was edited after release fails verification rather than looking
plausible. Verification recomputes from the bytes on disk; comparing a recorded size, or trusting a
filename, would pass for a file whose contents changed.

Three exports, each answering a different question:

* `events.jsonl` — ordered by aggregate and sequence, never by timestamp, so the campaign's state is
  reconstructable from the file alone (§5.2);
* `observations.json` — what was received, with origin, mode, checksum and lineage root;
* `metrics.json` — what was derived, with the analysis name, version and parameter hash that
  produced it, and with rejected metrics included, because a bundle that showed only the accepted
  ones would misrepresent the campaign.

Nothing here writes to object storage. A bundle is built on the filesystem and uploaded by the
caller, so building one is testable without a store.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from sqlalchemy import Connection, select

from labbridge.infrastructure.persistence.tables import (
    campaigns,
    derived_metrics,
    observations,
)
from labbridge.runtime.events import read_stream

from .manifest import MANIFEST_FILENAME as _MANIFEST_FILENAME
from .manifest import (
    ArtifactVerificationError,
    build_manifest,
    canonical_json,
    digest,
    verify_manifest,
)

BUNDLE_SCHEMA_VERSION: Final = "1"
MANIFEST_FILENAME: Final = _MANIFEST_FILENAME
EVENTS_FILENAME: Final = "events.jsonl"
OBSERVATIONS_FILENAME: Final = "observations.json"
METRICS_FILENAME: Final = "metrics.json"


class BundleVerificationError(ArtifactVerificationError):
    """A bundle whose bytes no longer match its manifest. Never downgraded to a warning."""


_canonical_json = canonical_json
_digest = digest


def _write(destination: Path, name: str, data: bytes) -> tuple[str, str, int]:
    """Write one member and return its manifest entry. Binary, so no platform rewrites a newline."""
    (destination / name).write_bytes(data)
    return name, _digest(data), len(data)


def _observation_rows(connection: Connection, campaign_id: uuid.UUID) -> list[dict[str, object]]:
    rows = connection.execute(
        select(observations)
        .where(observations.c.campaign_id == campaign_id)
        .order_by(observations.c.observation_id, observations.c.attempt_id)
    ).mappings()
    return [
        {
            "observation_id": row["observation_id"],
            "attempt_id": str(row["attempt_id"]),
            "work_item_id": str(row["work_item_id"]),
            "sha256": row["sha256"],
            "byte_size": row["byte_size"],
            "object_uri": row["object_uri"],
            "media_type": row["media_type"],
            "schema_version": row["schema_version"],
            "signal_kind": row["signal_kind"],
            "quantities": row["quantities"],
            "status": row["status"],
            "status_reason": row["status_reason"],
            "data_origin": row["data_origin"],
            "execution_mode": row["execution_mode"],
            "provenance": row["provenance"],
        }
        for row in rows
    ]


def _metric_rows(connection: Connection, campaign_id: uuid.UUID) -> list[dict[str, object]]:
    rows = connection.execute(
        select(derived_metrics)
        .join(
            observations,
            (derived_metrics.c.observation_id == observations.c.observation_id)
            & (derived_metrics.c.attempt_id == observations.c.attempt_id),
        )
        .where(observations.c.campaign_id == campaign_id)
        .order_by(derived_metrics.c.metric_id)
    ).mappings()
    return [
        {
            "metric_id": row["metric_id"],
            "observation_id": row["observation_id"],
            "attempt_id": str(row["attempt_id"]),
            "name": row["name"],
            "value": row["value"],
            "unit": row["unit"],
            "normalisation_basis": row["normalisation_basis"],
            "analysis_name": row["analysis_name"],
            "analysis_version": row["analysis_version"],
            "parameter_hash": row["parameter_hash"],
            "quality_status": row["quality_status"],
            "quality_reason": row["quality_reason"],
            "provenance": row["provenance"],
        }
        for row in rows
    ]


def build_bundle(
    connection: Connection, campaign_id: uuid.UUID, destination: Path
) -> dict[str, object]:
    """Export one campaign into `destination` and return the manifest.

    The manifest records `data_origin` at the top level. A synthetic campaign's bundle must be
    identifiable as synthetic by a machine reading only the manifest, not just by a human reading a
    chart caption (`docs/SIMULATOR_MODEL.md` §13, F-045).
    """
    destination.mkdir(parents=True, exist_ok=True)
    campaign = (
        connection.execute(select(campaigns).where(campaigns.c.campaign_id == campaign_id))
        .mappings()
        .one()
    )

    events_payload = read_stream(connection, campaign_id)
    events_bytes = (
        "\n".join(
            json.dumps(event, sort_keys=True, ensure_ascii=False, default=str)
            for event in events_payload
        )
        + "\n"
    ).encode("utf-8")

    _write(destination, EVENTS_FILENAME, events_bytes)
    _write(
        destination,
        OBSERVATIONS_FILENAME,
        _canonical_json(_observation_rows(connection, campaign_id)),
    )
    _write(destination, METRICS_FILENAME, _canonical_json(_metric_rows(connection, campaign_id)))

    return build_manifest(
        destination,
        metadata={
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "campaign_id": str(campaign_id),
            "campaign_name": campaign["name"],
            "environment_id": campaign["environment_id"],
            "data_origin": campaign["data_origin"],
            "execution_mode": campaign["execution_mode"],
            "generated_at": datetime.now(UTC).isoformat(),
        },
    )


def verify_bundle(destination: Path) -> dict[str, object]:
    """Recompute every digest from the bytes on disk. Raises on the first inconsistency found.

    Recomputed rather than compared against a recorded size: a file edited in place keeps its size
    far more often than it keeps its hash, and a size check would pass for exactly the tampering
    this is meant to catch.
    """
    try:
        return verify_manifest(destination)
    except ArtifactVerificationError as error:
        raise BundleVerificationError(error.problems) from error

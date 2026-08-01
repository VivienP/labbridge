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

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, TypedDict, cast

from pydantic import ValidationError
from sqlalchemy import Connection, select

from labbridge.domain.identity import ADMISSIBLE_PAIRS
from labbridge.domain.provenance import Provenance
from labbridge.infrastructure.objectstore import ObjectNotFoundError, ObjectStore, ObjectStoreError
from labbridge.infrastructure.persistence.tables import (
    campaigns,
    derived_metrics,
    observations,
    storage_objects,
)
from labbridge.runtime.events import read_stream

BUNDLE_SCHEMA_VERSION: Final = "2"
MANIFEST_FILENAME: Final = "manifest.json"
EVENTS_FILENAME: Final = "events.jsonl"
OBSERVATIONS_FILENAME: Final = "observations.json"
METRICS_FILENAME: Final = "metrics.json"


class VerificationMode(StrEnum):
    BUNDLE_ONLY = "bundle-only"
    FULL = "full"


class VerificationStatus(StrEnum):
    PARTIAL = "partial"
    COMPLETE = "complete"


class VerificationCheck(StrEnum):
    BUNDLE_FILES = "bundle_files"
    OBJECT_EXISTENCE = "object_existence"
    OBJECT_BYTE_SIZE = "object_byte_size"
    OBJECT_SHA256 = "object_sha256"


class BundleErrorCode(StrEnum):
    BUNDLE_DESTINATION_EXISTS = "bundle_destination_exists"
    BUNDLE_GENERATED_AT_INVALID = "bundle_generated_at_invalid"
    BUNDLE_MANIFEST_MISSING = "bundle_manifest_missing"
    BUNDLE_MANIFEST_INVALID = "bundle_manifest_invalid"
    BUNDLE_MANIFEST_DIGEST_MISMATCH = "bundle_manifest_digest_mismatch"
    BUNDLE_IDENTITY_INVALID = "bundle_identity_invalid"
    BUNDLE_OBJECT_REFERENCE_MISMATCH = "bundle_object_reference_mismatch"
    BUNDLE_MANIFEST_UNSUPPORTED = "bundle_manifest_unsupported"
    BUNDLE_FILES_DIGEST_MISMATCH = "bundle_files_digest_mismatch"
    BUNDLE_OBJECTS_DIGEST_MISMATCH = "bundle_objects_digest_mismatch"
    BUNDLE_MEMBER_MISSING = "bundle_member_missing"
    BUNDLE_MEMBER_SIZE_MISMATCH = "bundle_member_size_mismatch"
    BUNDLE_MEMBER_SHA256_MISMATCH = "bundle_member_sha256_mismatch"
    BUNDLE_UNEXPECTED_MEMBER = "bundle_unexpected_member"
    FULL_VERIFICATION_REQUIRES_MANIFEST_V2 = "full_verification_requires_manifest_v2"
    FULL_VERIFICATION_REQUIRES_OBJECT_STORE = "full_verification_requires_object_store"
    OBJECT_MISSING = "object_missing"
    OBJECT_SIZE_MISMATCH = "object_size_mismatch"
    OBJECT_SHA256_MISMATCH = "object_sha256_mismatch"
    OBJECT_STORE_FAILURE = "object_store_failure"
    OBJECT_METADATA_INCONSISTENT = "object_metadata_inconsistent"


class ObjectInventoryEntry(TypedDict):
    bucket: str
    key: str
    sha256: str
    byte_size: int
    media_type: str
    lifecycle_state: str
    observation_id: str
    attempt_id: str
    object_uri: str


@dataclass(frozen=True)
class BundleFailure:
    code: BundleErrorCode
    message: str
    details: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "message": self.message,
            "details": self.details,
        }


@dataclass(frozen=True)
class BundleVerificationResult:
    mode: VerificationMode
    status: VerificationStatus
    manifest_version: str
    verified_checks: tuple[VerificationCheck, ...]
    bundle_files_verified: int
    objects_referenced: int
    objects_verified: int
    limitations: tuple[str, ...]
    manifest: dict[str, object]


class EvidenceBundleError(Exception):
    def __init__(self, failure: BundleFailure) -> None:
        self.failure = failure
        super().__init__(failure.message)

    @property
    def code(self) -> BundleErrorCode:
        return self.failure.code

    def to_dict(self) -> dict[str, object]:
        return self.failure.to_dict()


class BundleBuildError(EvidenceBundleError):
    pass


class BundleVerificationError(EvidenceBundleError):
    """A bundle failure that is never downgraded to a warning."""

    @property
    def problems(self) -> tuple[str, ...]:
        return (self.failure.message,)


_SHA256_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
_OBSERVATION_ID_PATTERN: Final = re.compile(r"^obs:[0-9a-f]{32}$")
_OBJECT_FIELDS: Final = {
    "bucket",
    "key",
    "sha256",
    "byte_size",
    "media_type",
    "lifecycle_state",
    "observation_id",
    "attempt_id",
    "object_uri",
}


def _canonical_json(payload: object) -> bytes:
    """Sorted, compact, UTF-8, no NaN. The same rules identity uses, for the same reason."""
    return json.dumps(
        payload, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False, default=str
    ).encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _failure(code: BundleErrorCode, message: str, **details: object) -> BundleFailure:
    return BundleFailure(code=code, message=message, details=details)


def _verification_error(
    code: BundleErrorCode, message: str, **details: object
) -> BundleVerificationError:
    return BundleVerificationError(_failure(code, message, **details))


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


def _object_rows(connection: Connection, campaign_id: uuid.UUID) -> list[ObjectInventoryEntry]:
    rows = connection.execute(
        select(
            observations.c.observation_id,
            observations.c.attempt_id,
            observations.c.media_type,
            observations.c.sha256.label("observation_sha256"),
            observations.c.byte_size.label("observation_byte_size"),
            observations.c.object_uri.label("observation_object_uri"),
            storage_objects.c.bucket,
            storage_objects.c.object_key,
            storage_objects.c.sha256.label("storage_sha256"),
            storage_objects.c.byte_size.label("storage_byte_size"),
            storage_objects.c.state,
            storage_objects.c.object_uri.label("storage_object_uri"),
        )
        .select_from(
            observations.outerjoin(
                storage_objects,
                observations.c.object_uri == storage_objects.c.object_uri,
            )
        )
        .where(observations.c.campaign_id == campaign_id)
        .order_by(
            observations.c.observation_id,
            observations.c.attempt_id,
            storage_objects.c.bucket,
            storage_objects.c.object_key,
            storage_objects.c.object_uri,
        )
    ).mappings()
    inventory: list[ObjectInventoryEntry] = []
    seen_observations: set[tuple[str, str]] = set()
    for row in rows:
        observation_id = str(row["observation_id"])
        attempt_id = str(row["attempt_id"])
        identity = (observation_id, attempt_id)
        if identity in seen_observations:
            raise BundleBuildError(
                _failure(
                    BundleErrorCode.BUNDLE_MANIFEST_INVALID,
                    "duplicate object inventory row for an observation attempt",
                    observation_id=observation_id,
                    attempt_id=attempt_id,
                )
            )
        seen_observations.add(identity)
        if any(
            row[field] is None
            for field in (
                "bucket",
                "object_key",
                "storage_sha256",
                "storage_byte_size",
                "state",
                "storage_object_uri",
            )
        ):
            raise BundleBuildError(
                _failure(
                    BundleErrorCode.OBJECT_METADATA_INCONSISTENT,
                    "observation has no matching storage-object metadata",
                    observation_id=observation_id,
                    attempt_id=attempt_id,
                )
            )
        bucket = str(row["bucket"])
        key = str(row["object_key"])
        storage_uri = str(row["storage_object_uri"])
        observation_uri = str(row["observation_object_uri"])
        expected_uri = f"s3://{bucket}/{key}"
        metadata_is_consistent = (
            row["observation_sha256"] == row["storage_sha256"]
            and row["observation_byte_size"] == row["storage_byte_size"]
            and observation_uri == storage_uri
            and storage_uri == expected_uri
            and row["state"] == "committed"
        )
        if not metadata_is_consistent:
            raise BundleBuildError(
                _failure(
                    BundleErrorCode.OBJECT_METADATA_INCONSISTENT,
                    "observation and storage-object metadata are inconsistent",
                    observation_id=observation_id,
                    attempt_id=attempt_id,
                    object_uri=observation_uri,
                )
            )
        inventory.append(
            {
                "bucket": bucket,
                "key": key,
                "sha256": str(row["observation_sha256"]),
                "byte_size": int(row["observation_byte_size"]),
                "media_type": str(row["media_type"]),
                "lifecycle_state": str(row["state"]),
                "observation_id": observation_id,
                "attempt_id": attempt_id,
                "object_uri": observation_uri,
            }
        )
    return inventory


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
    connection: Connection,
    campaign_id: uuid.UUID,
    destination: Path,
    *,
    generated_at: datetime,
) -> dict[str, object]:
    """Export one campaign into `destination` and return the manifest.

    The manifest records `data_origin` at the top level. A synthetic campaign's bundle must be
    identifiable as synthetic by a machine reading only the manifest, not just by a human reading a
    chart caption (`docs/SIMULATOR_MODEL.md` §13, F-045).
    """
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise BundleBuildError(
            _failure(
                BundleErrorCode.BUNDLE_GENERATED_AT_INVALID,
                "bundle generation timestamp must be timezone-aware",
            )
        )
    if destination.exists():
        raise BundleBuildError(
            _failure(
                BundleErrorCode.BUNDLE_DESTINATION_EXISTS,
                "bundle destination already exists",
                destination=str(destination),
            )
        )
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
    observations_bytes = _canonical_json(_observation_rows(connection, campaign_id))
    metrics_bytes = _canonical_json(_metric_rows(connection, campaign_id))
    objects = _object_rows(connection, campaign_id)

    destination.mkdir(parents=True, exist_ok=False)
    members = [
        _write(destination, EVENTS_FILENAME, events_bytes),
        _write(destination, OBSERVATIONS_FILENAME, observations_bytes),
        _write(destination, METRICS_FILENAME, metrics_bytes),
    ]

    files = [{"name": name, "sha256": digest, "byte_size": size} for name, digest, size in members]
    manifest: dict[str, object] = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "campaign_id": str(campaign_id),
        "campaign_name": campaign["name"],
        "environment_id": campaign["environment_id"],
        "data_origin": campaign["data_origin"],
        "execution_mode": campaign["execution_mode"],
        "generated_at": generated_at.astimezone(UTC).isoformat(),
        "files": files,
        "objects": objects,
        # A digest over the entries, so editing a file *and* its recorded hash still fails.
        "files_digest": _digest(_canonical_json(files)),
        "objects_digest": _digest(_canonical_json(objects)),
    }
    manifest["manifest_digest"] = _digest(_canonical_json(manifest))
    (destination / MANIFEST_FILENAME).write_bytes(_canonical_json(manifest))
    return manifest


def _load_manifest(destination: Path) -> dict[str, object]:
    manifest_path = destination / MANIFEST_FILENAME
    if not manifest_path.exists():
        raise _verification_error(
            BundleErrorCode.BUNDLE_MANIFEST_MISSING,
            f"{MANIFEST_FILENAME} is missing",
            path=str(manifest_path),
        )
    try:
        loaded: object = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise _verification_error(
            BundleErrorCode.BUNDLE_MANIFEST_INVALID,
            "manifest is not valid UTF-8 JSON",
            path=str(manifest_path),
        ) from error
    if not isinstance(loaded, dict):
        raise _verification_error(
            BundleErrorCode.BUNDLE_MANIFEST_INVALID,
            "manifest root must be an object",
        )
    return loaded


def _verify_bundle_files(destination: Path, manifest: dict[str, object]) -> int:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise _verification_error(
            BundleErrorCode.BUNDLE_MANIFEST_INVALID,
            "manifest has no file list",
        )

    if _digest(_canonical_json(files)) != manifest.get("files_digest"):
        raise _verification_error(
            BundleErrorCode.BUNDLE_FILES_DIGEST_MISMATCH,
            "files_digest does not match the recorded file entries",
        )

    listed: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise _verification_error(
                BundleErrorCode.BUNDLE_MANIFEST_INVALID,
                "file entry must be an object",
            )
        name = entry.get("name")
        expected_sha256 = entry.get("sha256")
        expected_size = entry.get("byte_size")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not isinstance(expected_sha256, str)
            or _SHA256_PATTERN.fullmatch(expected_sha256) is None
            or not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
        ):
            raise _verification_error(
                BundleErrorCode.BUNDLE_MANIFEST_INVALID,
                "file entry has invalid fields",
                name=str(name),
            )
        listed.add(name)
        member = destination / name
        if not member.exists():
            raise _verification_error(
                BundleErrorCode.BUNDLE_MEMBER_MISSING,
                f"{name} is listed in the manifest but missing",
                name=name,
            )
        data = member.read_bytes()
        if len(data) != expected_size:
            raise _verification_error(
                BundleErrorCode.BUNDLE_MEMBER_SIZE_MISMATCH,
                f"{name}: byte size {len(data)} does not match manifest {expected_size}",
                name=name,
                expected=expected_size,
                actual=len(data),
            )
        actual = _digest(data)
        if actual != expected_sha256:
            raise _verification_error(
                BundleErrorCode.BUNDLE_MEMBER_SHA256_MISMATCH,
                f"{name}: sha256 {actual} does not match manifest {expected_sha256}",
                name=name,
                expected=expected_sha256,
                actual=actual,
            )

    present = {path.name for path in destination.iterdir() if path.name != MANIFEST_FILENAME}
    for extra in sorted(present - listed):
        raise _verification_error(
            BundleErrorCode.BUNDLE_UNEXPECTED_MEMBER,
            f"{extra} is present but not listed in the manifest",
            name=extra,
        )
    return len(files)


def _verify_manifest_digest(manifest: dict[str, object]) -> None:
    covered = {key: value for key, value in manifest.items() if key != "manifest_digest"}
    if _digest(_canonical_json(covered)) != manifest.get("manifest_digest"):
        raise _verification_error(
            BundleErrorCode.BUNDLE_MANIFEST_DIGEST_MISMATCH,
            "manifest_digest does not match the recorded manifest fields",
        )


def _verify_manifest_identity(manifest: dict[str, object]) -> None:
    campaign_id = manifest.get("campaign_id")
    environment_id = manifest.get("environment_id")
    data_origin = manifest.get("data_origin")
    execution_mode = manifest.get("execution_mode")
    generated_at = manifest.get("generated_at")
    try:
        canonical_campaign_id = str(uuid.UUID(campaign_id)) if isinstance(campaign_id, str) else ""
    except ValueError:
        canonical_campaign_id = ""
    try:
        parsed_generated_at = (
            datetime.fromisoformat(generated_at) if isinstance(generated_at, str) else None
        )
    except ValueError:
        parsed_generated_at = None
    generated_at_is_aware = (
        parsed_generated_at is not None
        and parsed_generated_at.tzinfo is not None
        and parsed_generated_at.utcoffset() is not None
    )
    if (
        canonical_campaign_id != campaign_id
        or not isinstance(environment_id, str)
        or not environment_id
        or not isinstance(data_origin, str)
        or not isinstance(execution_mode, str)
        or (data_origin, execution_mode) not in ADMISSIBLE_PAIRS
        or not generated_at_is_aware
    ):
        raise _verification_error(
            BundleErrorCode.BUNDLE_IDENTITY_INVALID,
            "manifest identity fields are invalid",
        )


def _verify_object_references(
    destination: Path,
    manifest: dict[str, object],
    inventory: list[ObjectInventoryEntry],
) -> None:
    try:
        loaded: object = json.loads(
            (destination / OBSERVATIONS_FILENAME).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise _verification_error(
            BundleErrorCode.BUNDLE_OBJECT_REFERENCE_MISMATCH,
            "observations member cannot be read for object-reference validation",
        ) from error
    if not isinstance(loaded, list):
        raise _verification_error(
            BundleErrorCode.BUNDLE_OBJECT_REFERENCE_MISMATCH,
            "observations member must contain a list",
        )
    rows: dict[tuple[str, str], dict[str, object]] = {}
    for loaded_row in loaded:
        if not isinstance(loaded_row, dict):
            raise _verification_error(
                BundleErrorCode.BUNDLE_OBJECT_REFERENCE_MISMATCH,
                "observation export row must be an object",
            )
        observation_id = loaded_row.get("observation_id")
        attempt_id = loaded_row.get("attempt_id")
        if not isinstance(observation_id, str) or not isinstance(attempt_id, str):
            raise _verification_error(
                BundleErrorCode.BUNDLE_OBJECT_REFERENCE_MISMATCH,
                "observation export row has no canonical reference",
            )
        identity = (observation_id, attempt_id)
        if identity in rows:
            raise _verification_error(
                BundleErrorCode.BUNDLE_OBJECT_REFERENCE_MISMATCH,
                "observation export repeats a canonical reference",
                observation_id=observation_id,
                attempt_id=attempt_id,
            )
        rows[identity] = loaded_row

    inventory_by_identity = {
        (entry["observation_id"], entry["attempt_id"]): entry for entry in inventory
    }
    if set(rows) != set(inventory_by_identity):
        raise _verification_error(
            BundleErrorCode.BUNDLE_OBJECT_REFERENCE_MISMATCH,
            "object inventory and observations member reference different records",
        )

    manifest_environment = manifest["environment_id"]
    manifest_origin = manifest["data_origin"]
    manifest_mode = manifest["execution_mode"]
    for identity, entry in inventory_by_identity.items():
        row = rows[identity]
        try:
            provenance = Provenance.model_validate(row.get("provenance"))
        except ValidationError as error:
            raise _verification_error(
                BundleErrorCode.BUNDLE_OBJECT_REFERENCE_MISMATCH,
                "observation export has invalid provenance",
                observation_id=identity[0],
                attempt_id=identity[1],
            ) from error
        if not provenance.has_root:
            raise _verification_error(
                BundleErrorCode.BUNDLE_OBJECT_REFERENCE_MISMATCH,
                "observation export provenance has no lineage root",
                observation_id=identity[0],
                attempt_id=identity[1],
            )
        expected_fields = {
            "sha256": entry["sha256"],
            "byte_size": entry["byte_size"],
            "object_uri": entry["object_uri"],
            "media_type": entry["media_type"],
        }
        row_matches_object = all(
            row.get(field) == value for field, value in expected_fields.items()
        )
        row_matches_manifest = (
            row.get("data_origin") == manifest_origin and row.get("execution_mode") == manifest_mode
        )
        provenance_matches_manifest = (
            provenance.environment.environment_id == manifest_environment
            and provenance.environment.data_origin == manifest_origin
            and provenance.environment.execution_mode == manifest_mode
        )
        if not row_matches_object or not row_matches_manifest or not provenance_matches_manifest:
            raise _verification_error(
                BundleErrorCode.BUNDLE_OBJECT_REFERENCE_MISMATCH,
                "object inventory does not match its exported observation",
                observation_id=identity[0],
                attempt_id=identity[1],
            )


def _object_inventory(manifest: dict[str, object]) -> list[ObjectInventoryEntry]:
    raw_objects = manifest.get("objects")
    if not isinstance(raw_objects, list):
        raise _verification_error(
            BundleErrorCode.BUNDLE_MANIFEST_INVALID,
            "manifest version 2 has no object inventory",
        )
    if _digest(_canonical_json(raw_objects)) != manifest.get("objects_digest"):
        raise _verification_error(
            BundleErrorCode.BUNDLE_OBJECTS_DIGEST_MISMATCH,
            "objects_digest does not match the recorded object inventory",
        )
    inventory: list[ObjectInventoryEntry] = []
    observation_attempts: set[tuple[str, str]] = set()
    for raw_entry in raw_objects:
        if not isinstance(raw_entry, dict) or set(raw_entry) != _OBJECT_FIELDS:
            raise _verification_error(
                BundleErrorCode.BUNDLE_MANIFEST_INVALID,
                "object inventory entry has an invalid field set",
            )
        strings = (
            "bucket",
            "key",
            "sha256",
            "media_type",
            "lifecycle_state",
            "observation_id",
            "attempt_id",
            "object_uri",
        )
        if any(not isinstance(raw_entry[field], str) or not raw_entry[field] for field in strings):
            raise _verification_error(
                BundleErrorCode.BUNDLE_MANIFEST_INVALID,
                "object inventory entry has an invalid string field",
            )
        byte_size = raw_entry["byte_size"]
        attempt_id = raw_entry["attempt_id"]
        try:
            canonical_attempt_id = str(uuid.UUID(attempt_id))
        except ValueError:
            canonical_attempt_id = ""
        if (
            not isinstance(byte_size, int)
            or isinstance(byte_size, bool)
            or byte_size < 0
            or _SHA256_PATTERN.fullmatch(raw_entry["sha256"]) is None
            or raw_entry["lifecycle_state"] != "committed"
            or canonical_attempt_id != attempt_id
            or _OBSERVATION_ID_PATTERN.fullmatch(raw_entry["observation_id"]) is None
            or raw_entry["object_uri"] != f"s3://{raw_entry['bucket']}/{raw_entry['key']}"
        ):
            raise _verification_error(
                BundleErrorCode.BUNDLE_MANIFEST_INVALID,
                "object inventory entry has invalid evidence values",
            )
        identity = (raw_entry["observation_id"], raw_entry["attempt_id"])
        if identity in observation_attempts:
            raise _verification_error(
                BundleErrorCode.BUNDLE_MANIFEST_INVALID,
                "object inventory repeats an observation attempt",
                observation_id=identity[0],
                attempt_id=identity[1],
            )
        observation_attempts.add(identity)
        inventory.append(cast(ObjectInventoryEntry, raw_entry))

    expected_order = sorted(
        inventory,
        key=lambda entry: (
            entry["observation_id"],
            entry["attempt_id"],
            entry["bucket"],
            entry["key"],
            entry["object_uri"],
        ),
    )
    if inventory != expected_order:
        raise _verification_error(
            BundleErrorCode.BUNDLE_MANIFEST_INVALID,
            "object inventory is not in canonical order",
        )
    return inventory


def _verify_objects(inventory: list[ObjectInventoryEntry], store: ObjectStore) -> int:
    verified: dict[tuple[str, str], tuple[int, str]] = {}
    for entry in inventory:
        coordinates = (entry["bucket"], entry["key"])
        expected = (entry["byte_size"], entry["sha256"])
        if coordinates in verified:
            if verified[coordinates] != expected:
                raise _verification_error(
                    BundleErrorCode.BUNDLE_MANIFEST_INVALID,
                    "one physical object has conflicting inventory evidence",
                    bucket=entry["bucket"],
                    key=entry["key"],
                )
            continue
        if store.bucket != entry["bucket"]:
            raise _verification_error(
                BundleErrorCode.OBJECT_STORE_FAILURE,
                "configured object store cannot address the recorded bucket",
                bucket=entry["bucket"],
                key=entry["key"],
            )
        try:
            data = store.get(entry["key"])
        except ObjectNotFoundError as error:
            raise _verification_error(
                BundleErrorCode.OBJECT_MISSING,
                "recorded object is missing",
                bucket=entry["bucket"],
                key=entry["key"],
                observation_id=entry["observation_id"],
                attempt_id=entry["attempt_id"],
            ) from error
        except ObjectStoreError as error:
            raise _verification_error(
                BundleErrorCode.OBJECT_STORE_FAILURE,
                "object store failed while reading recorded content",
                bucket=entry["bucket"],
                key=entry["key"],
                store_code=error.code,
            ) from error
        if len(data) != entry["byte_size"]:
            raise _verification_error(
                BundleErrorCode.OBJECT_SIZE_MISMATCH,
                "recorded object byte size does not match stored content",
                bucket=entry["bucket"],
                key=entry["key"],
                expected=entry["byte_size"],
                actual=len(data),
            )
        actual = _digest(data)
        if actual != entry["sha256"]:
            raise _verification_error(
                BundleErrorCode.OBJECT_SHA256_MISMATCH,
                "recorded object SHA-256 does not match stored content",
                bucket=entry["bucket"],
                key=entry["key"],
                expected=entry["sha256"],
                actual=actual,
            )
        verified[coordinates] = expected
    return len(verified)


def verify_bundle(
    destination: Path,
    *,
    mode: VerificationMode,
    object_store: ObjectStore | None = None,
) -> BundleVerificationResult:
    """Verify bundle members locally and, in full mode, every recorded object byte."""
    manifest = _load_manifest(destination)
    version = manifest.get("schema_version")
    if version not in {"1", "2"}:
        raise _verification_error(
            BundleErrorCode.BUNDLE_MANIFEST_UNSUPPORTED,
            "bundle manifest version is unsupported",
            manifest_version=version,
        )
    if version == "2":
        _verify_manifest_digest(manifest)
        _verify_manifest_identity(manifest)
    bundle_files_verified = _verify_bundle_files(destination, manifest)
    inventory = _object_inventory(manifest) if version == "2" else []
    if version == "2":
        _verify_object_references(destination, manifest, inventory)

    if mode is VerificationMode.BUNDLE_ONLY:
        return BundleVerificationResult(
            mode=mode,
            status=VerificationStatus.PARTIAL,
            manifest_version=version,
            verified_checks=(VerificationCheck.BUNDLE_FILES,),
            bundle_files_verified=bundle_files_verified,
            objects_referenced=len(inventory),
            objects_verified=0,
            limitations=("Object-store existence, byte size, and SHA-256 were not checked.",),
            manifest=manifest,
        )

    if version != "2":
        raise _verification_error(
            BundleErrorCode.FULL_VERIFICATION_REQUIRES_MANIFEST_V2,
            "full verification requires manifest version 2",
            manifest_version=version,
        )
    if object_store is None:
        raise _verification_error(
            BundleErrorCode.FULL_VERIFICATION_REQUIRES_OBJECT_STORE,
            "full verification requires an object store",
        )
    objects_verified = _verify_objects(inventory, object_store)
    return BundleVerificationResult(
        mode=mode,
        status=VerificationStatus.COMPLETE,
        manifest_version=version,
        verified_checks=(
            VerificationCheck.BUNDLE_FILES,
            VerificationCheck.OBJECT_EXISTENCE,
            VerificationCheck.OBJECT_BYTE_SIZE,
            VerificationCheck.OBJECT_SHA256,
        ),
        bundle_files_verified=bundle_files_verified,
        objects_referenced=len(inventory),
        objects_verified=objects_verified,
        limitations=(),
        manifest=manifest,
    )

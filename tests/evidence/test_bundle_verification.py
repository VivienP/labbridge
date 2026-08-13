from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import Connection

from labbridge.evidence.bundle import (
    MANIFEST_FILENAME,
    BundleBuildError,
    BundleErrorCode,
    BundleVerificationError,
    VerificationCheck,
    VerificationMode,
    VerificationStatus,
    build_bundle,
    verify_bundle,
)
from labbridge.infrastructure.objectstore import InMemoryObjectStore


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False, default=str
    ).encode("utf-8")


def test_builder_requires_an_explicit_keyword_only_generation_timestamp() -> None:
    parameter = inspect.signature(build_bundle).parameters.get("generated_at")

    assert parameter is not None
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def _write_bundle(
    destination: Path,
    *,
    version: str,
    objects: list[dict[str, object]] | None = None,
) -> None:
    destination.mkdir()
    observation_rows = [
        {
            "observation_id": entry["observation_id"],
            "attempt_id": entry["attempt_id"],
            "sha256": entry["sha256"],
            "byte_size": entry["byte_size"],
            "object_uri": entry["object_uri"],
            "media_type": entry["media_type"],
            "data_origin": "synthetic",
            "execution_mode": "replay",
            "provenance": {
                "code_version": "1",
                "config_hash": "config-1",
                "environment": {
                    "environment_id": "her",
                    "data_origin": "synthetic",
                    "execution_mode": "replay",
                    "adapter_version": "1",
                },
                "parent_ids": [],
                "source_record": None,
                "synthetic_root": {
                    "generator": "test-fixture",
                    "generator_version": "1",
                    "seed": 0,
                    "config_hash": "fixture-config-1",
                    "component_versions": [],
                },
            },
        }
        for entry in objects or []
    ]
    member = _canonical_json(observation_rows)
    (destination / "observations.json").write_bytes(member)
    files = [
        {
            "name": "observations.json",
            "sha256": hashlib.sha256(member).hexdigest(),
            "byte_size": len(member),
        }
    ]
    manifest: dict[str, object] = {
        "schema_version": version,
        "campaign_id": str(uuid.uuid4()),
        "environment_id": "her",
        "data_origin": "synthetic",
        "execution_mode": "replay",
        "generated_at": "2026-08-01T00:00:00+00:00",
        "files": files,
        "files_digest": hashlib.sha256(_canonical_json(files)).hexdigest(),
    }
    if objects is not None:
        manifest["objects"] = objects
        manifest["objects_digest"] = hashlib.sha256(_canonical_json(objects)).hexdigest()
    if version == "2":
        manifest["manifest_digest"] = hashlib.sha256(_canonical_json(manifest)).hexdigest()
    (destination / MANIFEST_FILENAME).write_bytes(_canonical_json(manifest))


def _refresh_manifest_digest(manifest: dict[str, object]) -> None:
    manifest.pop("manifest_digest", None)
    manifest["manifest_digest"] = hashlib.sha256(_canonical_json(manifest)).hexdigest()


def _replace_observations(
    bundle: Path, manifest: dict[str, object], rows: list[dict[str, object]]
) -> None:
    data = _canonical_json(rows)
    (bundle / "observations.json").write_bytes(data)
    files = manifest["files"]
    assert isinstance(files, list)
    entry = next(item for item in files if item["name"] == "observations.json")
    entry["sha256"] = hashlib.sha256(data).hexdigest()
    entry["byte_size"] = len(data)
    manifest["files_digest"] = hashlib.sha256(_canonical_json(files)).hexdigest()
    _refresh_manifest_digest(manifest)


def _object_entry(
    *,
    key: str,
    payload: bytes,
    observation_id: str = f"obs:{'a' * 32}",
    attempt_id: str | None = None,
) -> dict[str, object]:
    return {
        "bucket": "labbridge",
        "key": key,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "byte_size": len(payload),
        "media_type": "application/octet-stream",
        "lifecycle_state": "committed",
        "observation_id": observation_id,
        "attempt_id": attempt_id or str(uuid.uuid4()),
        "object_uri": f"s3://labbridge/{key}",
    }


def test_version_1_bundle_only_verification_is_typed_and_partial(tmp_path: Path) -> None:
    _write_bundle(tmp_path / "bundle", version="1")

    result = verify_bundle(tmp_path / "bundle", mode=VerificationMode.BUNDLE_ONLY)

    assert result.mode is VerificationMode.BUNDLE_ONLY
    assert result.status is VerificationStatus.PARTIAL
    assert result.manifest_version == "1"
    assert result.verified_checks == (VerificationCheck.BUNDLE_FILES,)
    assert result.bundle_files_verified == 1
    assert result.objects_referenced == 0
    assert result.objects_verified == 0
    assert result.limitations == (
        "Object-store existence, byte size, and SHA-256 were not checked.",
    )


def test_version_1_full_verification_has_a_stable_structured_error(tmp_path: Path) -> None:
    _write_bundle(tmp_path / "bundle", version="1")

    with pytest.raises(BundleVerificationError) as caught:
        verify_bundle(
            tmp_path / "bundle",
            mode=VerificationMode.FULL,
            object_store=InMemoryObjectStore(),
        )

    assert caught.value.code is BundleErrorCode.FULL_VERIFICATION_REQUIRES_MANIFEST_V2
    assert caught.value.to_dict()["code"] == "full_verification_requires_manifest_v2"


def test_full_verification_requires_an_object_store(tmp_path: Path) -> None:
    _write_bundle(tmp_path / "bundle", version="2", objects=[])

    with pytest.raises(BundleVerificationError) as caught:
        verify_bundle(tmp_path / "bundle", mode=VerificationMode.FULL)

    assert caught.value.code is BundleErrorCode.FULL_VERIFICATION_REQUIRES_OBJECT_STORE


def test_full_verification_checks_size_and_sha256(tmp_path: Path) -> None:
    payload = b"recorded bytes"
    entry = _object_entry(key="observations/one.bin", payload=payload)
    _write_bundle(tmp_path / "bundle", version="2", objects=[entry])
    store = InMemoryObjectStore()
    store.put_and_verify("observations/one.bin", payload, media_type="application/octet-stream")

    result = verify_bundle(tmp_path / "bundle", mode=VerificationMode.FULL, object_store=store)

    assert result.status is VerificationStatus.COMPLETE
    assert result.verified_checks == (
        VerificationCheck.BUNDLE_FILES,
        VerificationCheck.OBJECT_EXISTENCE,
        VerificationCheck.OBJECT_BYTE_SIZE,
        VerificationCheck.OBJECT_SHA256,
    )
    assert result.objects_referenced == 1
    assert result.objects_verified == 1
    assert result.limitations == ()


@pytest.mark.parametrize(
    ("stored", "code"),
    [
        (None, BundleErrorCode.OBJECT_MISSING),
        (b"different size!", BundleErrorCode.OBJECT_SIZE_MISMATCH),
        (b"recorded bytez", BundleErrorCode.OBJECT_SHA256_MISMATCH),
    ],
)
def test_full_verification_reports_object_failures(
    tmp_path: Path, stored: bytes | None, code: BundleErrorCode
) -> None:
    payload = b"recorded bytes"
    entry = _object_entry(key="observations/one.bin", payload=payload)
    _write_bundle(tmp_path / "bundle", version="2", objects=[entry])
    store = InMemoryObjectStore()
    if stored is not None:
        store.put_and_verify("observations/one.bin", stored, media_type="application/octet-stream")

    with pytest.raises(BundleVerificationError) as caught:
        verify_bundle(tmp_path / "bundle", mode=VerificationMode.FULL, object_store=store)

    assert caught.value.code is code
    assert caught.value.to_dict()["details"]["key"] == "observations/one.bin"


def test_duplicate_references_are_retained_but_physical_bytes_are_read_once(tmp_path: Path) -> None:
    reference_count = 2

    class CountingStore(InMemoryObjectStore):
        def __init__(self) -> None:
            super().__init__()
            self.reads = 0

        def get(self, key: str) -> bytes:
            self.reads += 1
            return super().get(key)

    payload = b"shared bytes"
    objects = [
        _object_entry(
            key="observations/shared.bin", payload=payload, observation_id=f"obs:{'a' * 32}"
        ),
        _object_entry(
            key="observations/shared.bin", payload=payload, observation_id=f"obs:{'b' * 32}"
        ),
    ]
    _write_bundle(tmp_path / "bundle", version="2", objects=objects)
    store = CountingStore()
    store.put_and_verify("observations/shared.bin", payload, media_type="application/octet-stream")

    result = verify_bundle(tmp_path / "bundle", mode=VerificationMode.FULL, object_store=store)

    assert result.objects_referenced == reference_count
    assert result.objects_verified == 1
    assert store.reads == 1


def test_editing_an_object_entry_without_its_digest_fails_closed(tmp_path: Path) -> None:
    payload = b"recorded bytes"
    objects = [_object_entry(key="observations/one.bin", payload=payload)]
    bundle = tmp_path / "bundle"
    _write_bundle(bundle, version="2", objects=objects)
    manifest_path = bundle / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["objects"][0]["media_type"] = "text/plain"
    _refresh_manifest_digest(manifest)
    manifest_path.write_bytes(_canonical_json(manifest))

    with pytest.raises(BundleVerificationError) as caught:
        verify_bundle(bundle, mode=VerificationMode.BUNDLE_ONLY)

    assert caught.value.code.value == "bundle_objects_digest_mismatch"


def test_editing_an_entry_and_the_physical_object_does_not_bypass_inventory_digest(
    tmp_path: Path,
) -> None:
    original = b"recorded bytes"
    modified = b"modified bytes"
    objects = [_object_entry(key="observations/one.bin", payload=original)]
    bundle = tmp_path / "bundle"
    _write_bundle(bundle, version="2", objects=objects)
    manifest_path = bundle / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["objects"][0]["sha256"] = hashlib.sha256(modified).hexdigest()
    manifest["objects"][0]["byte_size"] = len(modified)
    _refresh_manifest_digest(manifest)
    manifest_path.write_bytes(_canonical_json(manifest))
    store = InMemoryObjectStore()
    store.put_and_verify("observations/one.bin", modified, media_type="application/octet-stream")

    with pytest.raises(BundleVerificationError) as caught:
        verify_bundle(bundle, mode=VerificationMode.FULL, object_store=store)

    assert caught.value.code.value == "bundle_objects_digest_mismatch"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lifecycle_state", "pending"),
        ("attempt_id", "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"),
        ("observation_id", "observation-1"),
        ("object_uri", "s3://different-bucket/observations/one.bin"),
    ],
)
def test_version_2_inventory_rejects_noncanonical_evidence_fields(
    tmp_path: Path, field: str, value: str
) -> None:
    payload = b"recorded bytes"
    entry = _object_entry(key="observations/one.bin", payload=payload)
    entry[field] = value
    bundle = tmp_path / "bundle"
    _write_bundle(bundle, version="2", objects=[entry])

    with pytest.raises(BundleVerificationError) as caught:
        verify_bundle(bundle, mode=VerificationMode.BUNDLE_ONLY)

    assert caught.value.code is BundleErrorCode.BUNDLE_MANIFEST_INVALID


@pytest.mark.parametrize("field", ["data_origin", "campaign_id", "generated_at"])
def test_manifest_digest_detects_top_level_tampering(tmp_path: Path, field: str) -> None:
    bundle = tmp_path / "bundle"
    _write_bundle(bundle, version="2", objects=[])
    manifest_path = bundle / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = "tampered"
    manifest_path.write_bytes(_canonical_json(manifest))

    with pytest.raises(BundleVerificationError) as caught:
        verify_bundle(bundle, mode=VerificationMode.BUNDLE_ONLY)

    assert caught.value.code.value == "bundle_manifest_digest_mismatch"


@pytest.mark.parametrize(
    "updates",
    [
        {"campaign_id": "not-a-uuid"},
        {"environment_id": ""},
        {"data_origin": "observed", "execution_mode": "simulation"},
        {"generated_at": "2026-08-01T00:00:00"},
    ],
)
def test_version_2_rejects_invalid_top_level_identity_after_digest_refresh(
    tmp_path: Path, updates: dict[str, object]
) -> None:
    bundle = tmp_path / "bundle"
    _write_bundle(bundle, version="2", objects=[])
    manifest_path = bundle / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(updates)
    _refresh_manifest_digest(manifest)
    manifest_path.write_bytes(_canonical_json(manifest))

    with pytest.raises(BundleVerificationError) as caught:
        verify_bundle(bundle, mode=VerificationMode.BUNDLE_ONLY)

    assert caught.value.code.value == "bundle_identity_invalid"


@pytest.mark.parametrize("contract_version", [1, 2])
def test_version_2_accepts_complete_known_event_stream_contracts(
    tmp_path: Path, contract_version: int
) -> None:
    bundle = tmp_path / "bundle"
    _write_bundle(bundle, version="2", objects=[])
    manifest_path = bundle / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["event_stream_contract_version"] = contract_version
    manifest["event_stream_completeness"] = "complete"
    _refresh_manifest_digest(manifest)
    manifest_path.write_bytes(_canonical_json(manifest))

    result = verify_bundle(bundle, mode=VerificationMode.BUNDLE_ONLY)

    assert result.status is VerificationStatus.PARTIAL


@pytest.mark.parametrize("mutation", ["missing", "row_origin", "provenance_environment"])
def test_version_2_cross_checks_object_references_against_observations(
    tmp_path: Path, mutation: str
) -> None:
    payload = b"recorded bytes"
    bundle = tmp_path / "bundle"
    _write_bundle(
        bundle,
        version="2",
        objects=[_object_entry(key="observations/one.bin", payload=payload)],
    )
    manifest_path = bundle / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = json.loads((bundle / "observations.json").read_text(encoding="utf-8"))
    if mutation == "missing":
        rows = []
    elif mutation == "row_origin":
        rows[0]["data_origin"] = "observed"
    else:
        rows[0]["provenance"]["environment"]["environment_id"] = "different"
    _replace_observations(bundle, manifest, rows)
    manifest_path.write_bytes(_canonical_json(manifest))

    with pytest.raises(BundleVerificationError) as caught:
        verify_bundle(bundle, mode=VerificationMode.BUNDLE_ONLY)

    assert caught.value.code.value == "bundle_object_reference_mismatch"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_root",
        "double_roots",
        "root_incompatible_with_origin",
        "missing_adapter_version",
        "missing_code_version",
        "missing_config_hash",
    ],
)
def test_version_2_rejects_invalid_observation_provenance_after_digest_refresh(
    tmp_path: Path, mutation: str
) -> None:
    payload = b"recorded bytes"
    bundle = tmp_path / "bundle"
    _write_bundle(
        bundle,
        version="2",
        objects=[_object_entry(key="observations/one.bin", payload=payload)],
    )
    manifest_path = bundle / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = json.loads((bundle / "observations.json").read_text(encoding="utf-8"))
    provenance = rows[0]["provenance"]
    source_record = {
        "doi": "10.0000/example",
        "record_version": "1",
        "source_filename": "source.csv",
        "source_sha256": "0" * 64,
        "source_path": "source.csv",
        "source_type": "measured_lsv",
        "parsing_version": "1",
    }
    if mutation == "missing_root":
        provenance["synthetic_root"] = None
    elif mutation == "double_roots":
        provenance["source_record"] = source_record
    elif mutation == "root_incompatible_with_origin":
        provenance["synthetic_root"] = None
        provenance["source_record"] = source_record
    elif mutation == "missing_adapter_version":
        provenance["environment"].pop("adapter_version")
    elif mutation == "missing_code_version":
        provenance.pop("code_version")
    else:
        provenance.pop("config_hash")
    _replace_observations(bundle, manifest, rows)
    manifest_path.write_bytes(_canonical_json(manifest))

    with pytest.raises(BundleVerificationError) as caught:
        verify_bundle(bundle, mode=VerificationMode.BUNDLE_ONLY)

    assert caught.value.code.value == "bundle_object_reference_mismatch"


def test_builder_refuses_an_existing_destination_without_modifying_it(tmp_path: Path) -> None:
    destination = tmp_path / "bundle"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("unchanged", encoding="utf-8")

    with pytest.raises(BundleBuildError) as caught:
        build_bundle(
            cast(Connection, object()),
            uuid.uuid4(),
            destination,
            generated_at=datetime(2026, 8, 1, tzinfo=UTC),
        )

    assert caught.value.code is BundleErrorCode.BUNDLE_DESTINATION_EXISTS
    assert marker.read_text(encoding="utf-8") == "unchanged"
    assert sorted(path.name for path in destination.iterdir()) == ["keep.txt"]

"""The evidence bundle and the `labbridge demo her` run, end to end.

The demonstration requires that `labbridge demo her` complete and that the resulting bundle
verify. Both are exercised here against real PostgreSQL and MinIO, because a bundle built from a
mocked database would prove nothing about what a campaign actually recorded.

Tamper detection is tested by tampering. A manifest that is only ever verified against untouched
files establishes that the happy path works, not that the check does anything.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, delete, select, update

from labbridge.demo import run_demo
from labbridge.environments.her_replay import HerReplayAdapter
from labbridge.evidence.bundle import (
    EVENTS_FILENAME,
    MANIFEST_FILENAME,
    METRICS_FILENAME,
    OBSERVATIONS_FILENAME,
    BundleBuildError,
    BundleErrorCode,
    BundleVerificationError,
    VerificationMode,
    VerificationStatus,
    build_bundle,
    verify_bundle,
)
from labbridge.infrastructure.her_ingestion.fixture import (
    FIXTURE_MANIFEST_FILENAME,
    FixtureSpec,
    build_fixture,
)
from labbridge.infrastructure.her_ingestion.provenance import write_document
from labbridge.infrastructure.objectstore import S3ObjectStore
from labbridge.infrastructure.persistence.tables import (
    attempt_outcomes,
    observations,
    storage_objects,
)

pytestmark = pytest.mark.integration

SPEC = FixtureSpec(areas_per_library=6, seccm_areas_per_library=2)
LOCATIONS = 2
EXPECTED_MEMBERS = 3
COMPLETE_EVENT_STREAM_CONTRACT = 2
GENERATED_AT = datetime(2026, 8, 1, tzinfo=UTC)


@pytest.fixture(scope="session")
def demo_fixture_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("demo-fixture")
    manifest = build_fixture(root, spec=SPEC, generator_version="0.1.0")
    write_document(root / FIXTURE_MANIFEST_FILENAME, manifest)
    return root


@pytest.fixture
def demo(
    migrated: Engine,
    demo_fixture_root: Path,
    object_store: S3ObjectStore,
    tmp_path: Path,
    purge_campaign: Callable[[Connection, uuid.UUID], None],
) -> Iterator[tuple[Engine, Path, uuid.UUID]]:
    """Run one demonstration and clean up after it, in foreign-key order."""
    adapter = HerReplayAdapter(demo_fixture_root)
    report = asyncio.run(run_demo(migrated, adapter, object_store, tmp_path, locations=LOCATIONS))
    yield migrated, report.bundle_path, report.campaign_id
    with migrated.begin() as connection:
        purge_campaign(connection, report.campaign_id)


def test_the_demonstration_produces_a_bundle_that_verifies(
    demo: tuple[Engine, Path, uuid.UUID],
    object_store: S3ObjectStore,
) -> None:
    """The whole path, run rather than argued."""
    _, bundle_path, _ = demo

    result = verify_bundle(bundle_path, mode=VerificationMode.FULL, object_store=object_store)

    assert result.status is VerificationStatus.COMPLETE
    assert result.bundle_files_verified == EXPECTED_MEMBERS
    assert result.objects_referenced == LOCATIONS
    assert result.objects_verified == LOCATIONS
    for name in (EVENTS_FILENAME, OBSERVATIONS_FILENAME, METRICS_FILENAME):
        assert (bundle_path / name).exists()


def test_the_manifest_declares_the_origin_machine_readably(
    demo: tuple[Engine, Path, uuid.UUID],
) -> None:
    """F-045: a synthetic export must be identifiable by a machine reading the manifest, not only
    by a human reading a caption."""
    _, bundle_path, _ = demo

    manifest = json.loads((bundle_path / MANIFEST_FILENAME).read_text(encoding="utf-8"))

    assert manifest["data_origin"] == "synthetic"
    assert manifest["execution_mode"] == "replay"
    assert manifest["event_stream_contract_version"] == COMPLETE_EVENT_STREAM_CONTRACT
    assert manifest["event_stream_completeness"] == "complete"


def test_editing_a_member_breaks_verification(demo: tuple[Engine, Path, uuid.UUID]) -> None:
    """The check has to fail on a change, or it is decoration."""
    _, bundle_path, _ = demo
    member = bundle_path / METRICS_FILENAME
    member.write_bytes(member.read_bytes() + b"\n")

    with pytest.raises(BundleVerificationError, match="does not match manifest"):
        verify_bundle(bundle_path, mode=VerificationMode.BUNDLE_ONLY)


def test_editing_a_member_and_its_recorded_hash_still_breaks_verification(
    demo: tuple[Engine, Path, uuid.UUID],
) -> None:
    """The files_digest exists for this: correcting the per-file hash after tampering must not make
    the bundle verify again."""
    _, bundle_path, _ = demo
    member = bundle_path / METRICS_FILENAME
    tampered = member.read_bytes() + b"\n"
    member.write_bytes(tampered)

    manifest_path = bundle_path / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["name"] == METRICS_FILENAME:
            entry["sha256"] = hashlib.sha256(tampered).hexdigest()
            entry["byte_size"] = len(tampered)
    manifest.pop("manifest_digest")
    covered = json.dumps(
        manifest,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    manifest["manifest_digest"] = hashlib.sha256(covered).hexdigest()
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8")

    with pytest.raises(BundleVerificationError, match="files_digest"):
        verify_bundle(bundle_path, mode=VerificationMode.BUNDLE_ONLY)


def test_an_added_file_breaks_verification(demo: tuple[Engine, Path, uuid.UUID]) -> None:
    """A bundle is a closed set. Something added after release is what a manifest exists to show."""
    _, bundle_path, _ = demo
    (bundle_path / "extra.json").write_text("{}", encoding="utf-8")

    with pytest.raises(BundleVerificationError, match="not listed in the manifest"):
        verify_bundle(bundle_path, mode=VerificationMode.BUNDLE_ONLY)


def test_a_removed_file_breaks_verification(demo: tuple[Engine, Path, uuid.UUID]) -> None:
    _, bundle_path, _ = demo
    (bundle_path / METRICS_FILENAME).unlink()

    with pytest.raises(BundleVerificationError, match="missing"):
        verify_bundle(bundle_path, mode=VerificationMode.BUNDLE_ONLY)


def test_manifest_v2_inventory_matches_the_observation_storage_join(
    demo: tuple[Engine, Path, uuid.UUID],
) -> None:
    engine, bundle_path, campaign_id = demo
    manifest = json.loads((bundle_path / MANIFEST_FILENAME).read_text(encoding="utf-8"))

    with engine.begin() as connection:
        rows = connection.execute(
            select(
                observations.c.observation_id,
                observations.c.attempt_id,
                observations.c.media_type,
                storage_objects.c.bucket,
                storage_objects.c.object_key,
                storage_objects.c.sha256,
                storage_objects.c.byte_size,
                storage_objects.c.state,
                storage_objects.c.object_uri,
            )
            .select_from(
                observations.join(
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
            )
        ).mappings()
        expected = [
            {
                "bucket": row["bucket"],
                "key": row["object_key"],
                "sha256": row["sha256"],
                "byte_size": row["byte_size"],
                "media_type": row["media_type"],
                "lifecycle_state": row["state"],
                "observation_id": row["observation_id"],
                "attempt_id": str(row["attempt_id"]),
                "object_uri": row["object_uri"],
            }
            for row in rows
        ]

    assert manifest["schema_version"] == "2"
    assert manifest["objects"] == expected
    assert all(
        set(entry)
        == {
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
        for entry in manifest["objects"]
    )


def test_building_again_refuses_the_existing_destination(
    demo: tuple[Engine, Path, uuid.UUID],
) -> None:
    engine, bundle_path, campaign_id = demo
    before = {path.name: path.read_bytes() for path in bundle_path.iterdir()}

    with engine.begin() as connection, pytest.raises(BundleBuildError) as caught:
        build_bundle(connection, campaign_id, bundle_path, generated_at=GENERATED_AT)

    assert caught.value.code is BundleErrorCode.BUNDLE_DESTINATION_EXISTS
    assert {path.name: path.read_bytes() for path in bundle_path.iterdir()} == before


def test_same_snapshot_and_generation_timestamp_produce_the_same_manifest(
    demo: tuple[Engine, Path, uuid.UUID], tmp_path: Path
) -> None:
    engine, _, campaign_id = demo
    first = tmp_path / "first"
    second = tmp_path / "second"

    with engine.begin() as connection:
        build_bundle(connection, campaign_id, first, generated_at=GENERATED_AT)
        build_bundle(connection, campaign_id, second, generated_at=GENERATED_AT)

    assert (first / MANIFEST_FILENAME).read_bytes() == (second / MANIFEST_FILENAME).read_bytes()


def test_builder_rejects_a_naive_generation_timestamp(
    demo: tuple[Engine, Path, uuid.UUID], tmp_path: Path
) -> None:
    engine, _, campaign_id = demo

    with engine.begin() as connection, pytest.raises(BundleBuildError) as caught:
        build_bundle(
            connection,
            campaign_id,
            tmp_path / "naive-timestamp",
            generated_at=datetime(2026, 8, 1),
        )

    assert caught.value.code.value == "bundle_generated_at_invalid"


@pytest.mark.parametrize(
    "inconsistency", ["sha256", "byte_size", "state", "coordinates", "missing"]
)
def test_builder_rejects_inconsistent_observation_object_metadata(
    demo: tuple[Engine, Path, uuid.UUID], tmp_path: Path, inconsistency: str
) -> None:
    engine, _, campaign_id = demo
    destination = tmp_path / f"inconsistent-{inconsistency}"
    with engine.connect() as connection:
        transaction = connection.begin()
        row = (
            connection.execute(
                select(storage_objects)
                .join(observations, observations.c.object_uri == storage_objects.c.object_uri)
                .where(observations.c.campaign_id == campaign_id)
                .limit(1)
            )
            .mappings()
            .one()
        )
        if inconsistency == "missing":
            connection.execute(
                delete(storage_objects).where(storage_objects.c.object_uri == row["object_uri"])
            )
        else:
            values: dict[str, object]
            if inconsistency == "sha256":
                values = {"sha256": "0" * 64}
            elif inconsistency == "byte_size":
                values = {"byte_size": row["byte_size"] + 1}
            elif inconsistency == "state":
                values = {"state": "pending", "committed_at": None}
            else:
                values = {"object_key": f"{row['object_key']}.different"}
            connection.execute(
                update(storage_objects)
                .where(storage_objects.c.object_uri == row["object_uri"])
                .values(**values)
            )

        with pytest.raises(BundleBuildError) as caught:
            build_bundle(connection, campaign_id, destination, generated_at=GENERATED_AT)
        transaction.rollback()

    assert caught.value.code.value == "object_metadata_inconsistent"
    assert not destination.exists()


def test_full_verification_reports_a_deleted_minio_object(
    demo: tuple[Engine, Path, uuid.UUID], object_store: S3ObjectStore
) -> None:
    engine, bundle_path, _ = demo
    manifest = json.loads((bundle_path / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    entry = manifest["objects"][0]
    original = object_store.get(entry["key"])
    object_store._client.delete_object(Bucket=entry["bucket"], Key=entry["key"])
    try:
        with pytest.raises(BundleVerificationError) as caught:
            verify_bundle(bundle_path, mode=VerificationMode.FULL, object_store=object_store)
        assert caught.value.code is BundleErrorCode.OBJECT_MISSING
        with engine.begin() as connection:
            retained_uri = connection.execute(
                select(storage_objects.c.object_uri).where(
                    storage_objects.c.object_uri == entry["object_uri"]
                )
            ).scalar_one()
        assert retained_uri == entry["object_uri"]
    finally:
        object_store._client.put_object(Bucket=entry["bucket"], Key=entry["key"], Body=original)


def test_full_verification_reports_a_modified_minio_object(
    demo: tuple[Engine, Path, uuid.UUID], object_store: S3ObjectStore
) -> None:
    _, bundle_path, _ = demo
    manifest = json.loads((bundle_path / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    entry = manifest["objects"][0]
    original = object_store.get(entry["key"])
    modified = bytes([original[0] ^ 1]) + original[1:]
    object_store._client.put_object(Bucket=entry["bucket"], Key=entry["key"], Body=modified)
    try:
        with pytest.raises(BundleVerificationError) as caught:
            verify_bundle(bundle_path, mode=VerificationMode.FULL, object_store=object_store)
        assert caught.value.code is BundleErrorCode.OBJECT_SHA256_MISMATCH
    finally:
        object_store._client.put_object(Bucket=entry["bucket"], Key=entry["key"], Body=original)


def test_the_bundle_records_both_the_successes_and_the_terminal_failure(
    demo: tuple[Engine, Path, uuid.UUID],
) -> None:
    """A bundle that showed only what worked would misrepresent the campaign. The demonstration
    submits one location for which the replay source has no LSV precisely so this is observable."""
    engine, bundle_path, campaign_id = demo

    observations_payload = json.loads(
        (bundle_path / OBSERVATIONS_FILENAME).read_text(encoding="utf-8")
    )
    with engine.begin() as connection:
        statuses = set(
            connection.execute(
                select(attempt_outcomes.c.status).where(
                    attempt_outcomes.c.campaign_id == campaign_id
                )
            ).scalars()
        )

    assert len(observations_payload) == LOCATIONS
    assert statuses == {"succeeded", "failed_terminal"}


def test_every_exported_observation_carries_its_lineage_root(
    demo: tuple[Engine, Path, uuid.UUID],
) -> None:
    """docs/DATA_STRATEGY.md §6: an exported record that resolves to no root is uninterpretable."""
    _, bundle_path, _ = demo

    payload = json.loads((bundle_path / OBSERVATIONS_FILENAME).read_text(encoding="utf-8"))

    assert payload
    for row in payload:
        assert row["data_origin"] == "synthetic"
        assert row["provenance"]["synthetic_root"]["seed"] is not None
        assert row["provenance"]["source_record"] is None
        assert len(row["sha256"]) == 64  # noqa: PLR2004 - a SHA-256 hex digest


def test_the_event_stream_is_ordered_by_sequence(demo: tuple[Engine, Path, uuid.UUID]) -> None:
    """§5.1: replay orders by aggregate and sequence, never by timestamp."""
    _, bundle_path, _ = demo

    lines = (bundle_path / EVENTS_FILENAME).read_text(encoding="utf-8").strip().splitlines()
    parsed = [json.loads(line) for line in lines]

    assert parsed
    by_aggregate: dict[str, list[int]] = {}
    for event in parsed:
        by_aggregate.setdefault(event["aggregate_id"], []).append(event["sequence"])
    for sequences in by_aggregate.values():
        assert sequences == sorted(sequences)

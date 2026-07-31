"""The evidence bundle and the Slice 1 demonstration, end to end.

`docs/ROADMAP.md` Slice 1 asks that `labbridge demo her` complete and that the resulting bundle
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
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, select

from labbridge.demo import run_demo
from labbridge.environments.her_replay import HerReplayAdapter
from labbridge.evidence.bundle import (
    EVENTS_FILENAME,
    MANIFEST_FILENAME,
    METRICS_FILENAME,
    OBSERVATIONS_FILENAME,
    BundleVerificationError,
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
)

pytestmark = pytest.mark.integration

SPEC = FixtureSpec(areas_per_library=6, seccm_areas_per_library=2)
LOCATIONS = 2
EXPECTED_MEMBERS = 3


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
) -> None:
    """The Slice 1 exit criterion, run rather than argued."""
    _, bundle_path, _ = demo

    manifest = verify_bundle(bundle_path)

    assert len(manifest["files"]) == EXPECTED_MEMBERS  # type: ignore[arg-type]
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


def test_editing_a_member_breaks_verification(demo: tuple[Engine, Path, uuid.UUID]) -> None:
    """The check has to fail on a change, or it is decoration."""
    _, bundle_path, _ = demo
    member = bundle_path / METRICS_FILENAME
    member.write_bytes(member.read_bytes() + b"\n")

    with pytest.raises(BundleVerificationError, match="does not match manifest"):
        verify_bundle(bundle_path)


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
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8")

    with pytest.raises(BundleVerificationError, match="files_digest"):
        verify_bundle(bundle_path)


def test_an_added_file_breaks_verification(demo: tuple[Engine, Path, uuid.UUID]) -> None:
    """A bundle is a closed set. Something added after release is what a manifest exists to show."""
    _, bundle_path, _ = demo
    (bundle_path / "extra.json").write_text("{}", encoding="utf-8")

    with pytest.raises(BundleVerificationError, match="not listed in the manifest"):
        verify_bundle(bundle_path)


def test_a_removed_file_breaks_verification(demo: tuple[Engine, Path, uuid.UUID]) -> None:
    _, bundle_path, _ = demo
    (bundle_path / METRICS_FILENAME).unlink()

    with pytest.raises(BundleVerificationError, match="missing"):
        verify_bundle(bundle_path)


def test_the_bundle_records_both_the_successes_and_the_terminal_failure(
    demo: tuple[Engine, Path, uuid.UUID],
) -> None:
    """A bundle that showed only what worked would misrepresent the campaign. The demonstration
    submits one location the source never measured precisely so this is observable."""
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

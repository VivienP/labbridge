from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import boto3
import pytest
from botocore.config import Config as BotoConfig
from sqlalchemy import Connection, Engine, select

from labbridge.demo import run_demo
from labbridge.environments.her_replay import HerReplayAdapter
from labbridge.evidence.campaign_package import (
    build_campaign_experiment_package,
    campaign_package_inputs_from_postgres,
)
from labbridge.evidence.experiment_package import (
    ExperimentPackageVerificationError,
    verify_experiment_package,
)
from labbridge.infrastructure.her_ingestion.fixture import (
    FIXTURE_MANIFEST_FILENAME,
    FixtureSpec,
    build_fixture,
)
from labbridge.infrastructure.her_ingestion.provenance import write_document
from labbridge.infrastructure.objectstore import S3ObjectStore
from labbridge.infrastructure.persistence.config import ObjectStoreSettings
from labbridge.infrastructure.persistence.tables import (
    attempt_outcomes,
    events,
    observations,
    record_relations,
    storage_objects,
)
from labbridge.runtime.jobs import enqueue
from labbridge.runtime.worker import Worker

pytestmark = pytest.mark.integration
EXPECTED_ATTEMPTS_WITH_REDELIVERY = 3
EXPECTED_REFERENCED_OBJECTS = 2


@pytest.fixture(scope="session")
def campaign_package_fixture_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("campaign-package-fixture")
    manifest = build_fixture(
        root,
        spec=FixtureSpec(areas_per_library=4, seccm_areas_per_library=2),
        generator_version="0.1.0",
    )
    write_document(root / FIXTURE_MANIFEST_FILENAME, manifest)
    return root


@pytest.fixture
def completed_campaign(
    migrated: Engine,
    campaign_package_fixture_root: Path,
    object_store: S3ObjectStore,
    tmp_path: Path,
    purge_campaign: Callable[[Connection, uuid.UUID], None],
) -> Iterator[tuple[Engine, uuid.UUID]]:
    report = asyncio.run(
        run_demo(
            migrated,
            HerReplayAdapter(campaign_package_fixture_root),
            object_store,
            tmp_path,
            locations=1,
            include_unmeasured=True,
        )
    )
    with migrated.begin() as connection:
        accepted_item = connection.execute(
            select(attempt_outcomes.c.work_item_id).where(
                attempt_outcomes.c.campaign_id == report.campaign_id,
                attempt_outcomes.c.status == "succeeded",
            )
        ).scalar_one()
        context = connection.execute(
            select(events.c.event_id, events.c.correlation_id)
            .where(
                events.c.campaign_id == report.campaign_id,
                events.c.aggregate_id == accepted_item,
            )
            .order_by(events.c.sequence.desc())
            .limit(1)
        ).one()
        enqueue(
            connection,
            campaign_id=report.campaign_id,
            work_item_id=accepted_item,
            instruction_key=f"campaign-package-redelivery:{uuid.uuid4().hex}",
            command_version="1",
            correlation_id=context.correlation_id,
            causation_id=context.event_id,
        )
    duplicate = asyncio.run(
        Worker(
            migrated,
            HerReplayAdapter(campaign_package_fixture_root),
            object_store,
            name="campaign-package-redelivery",
        ).run_once()
    )
    assert duplicate is not None and duplicate.status == "duplicate_suppressed"
    yield migrated, report.campaign_id
    with migrated.begin() as connection:
        purge_campaign(connection, report.campaign_id)


def test_campaign_package_builds_from_postgres_and_fully_verifies_minio_objects(
    completed_campaign: tuple[Engine, uuid.UUID], object_store: S3ObjectStore
) -> None:
    engine, campaign_id = completed_campaign
    with engine.begin() as connection:
        inputs = campaign_package_inputs_from_postgres(
            connection,
            campaign_id,
            producing_versions={"labbridge": "0.1.0", "campaign_package": "1"},
            limitations=["Fixture-backed replay does not establish physical-system performance."],
        )

    package = build_campaign_experiment_package(inputs)
    verification = verify_experiment_package(package.archive_bytes, object_store=object_store)

    assert verification.producer_kind == "campaign"
    assert verification.lineage_closed is True
    assert verification.verification_scope == "full"
    assert verification.objects_referenced == EXPECTED_REFERENCED_OBJECTS
    assert verification.objects_verified == 1
    assert {row["status"] for row in inputs.attempts_outcomes} == {
        "succeeded",
        "failed_terminal",
        "duplicate_suppressed",
    }
    assert len(inputs.raw_results) == EXPECTED_ATTEMPTS_WITH_REDELIVERY
    duplicate = next(
        row for row in inputs.attempts_outcomes if row["status"] == "duplicate_suppressed"
    )
    retained = next(
        row for row in inputs.observations if row["attempt_id"] == duplicate["attempt_id"]
    )
    assert retained["status"] == "received"
    assert not any(row["attempt_id"] == duplicate["attempt_id"] for row in inputs.derived_metrics)
    assert not any(
        row["event_type"] == "observation.accepted"
        and row["aggregate_id"] == duplicate["attempt_id"]
        for row in inputs.events
    )
    for metric in inputs.derived_metrics:
        assert metric["environment_id"] == inputs.environment["environment_id"]
        assert metric["data_origin"] == inputs.environment["data_origin"]
        assert metric["execution_mode"] == inputs.environment["execution_mode"]


def test_campaign_package_postgres_adapter_does_not_export_an_open_relation(
    completed_campaign: tuple[Engine, uuid.UUID],
) -> None:
    engine, campaign_id = completed_campaign
    relation_id = uuid.uuid4()
    try:
        with engine.begin() as connection:
            observation_identity = connection.execute(
                select(observations.c.observation_id)
                .where(observations.c.campaign_id == campaign_id)
                .limit(1)
            ).scalar_one()
            recorded_at = connection.execute(
                select(events.c.recorded_at)
                .where(events.c.campaign_id == campaign_id)
                .order_by(events.c.campaign_position)
                .limit(1)
            ).scalar_one()
            connection.execute(
                record_relations.insert().values(
                    relation_id=relation_id,
                    subject_id=observation_identity,
                    predicate="invalidates",
                    object_id="obs:outside-package",
                    reason="fixture open edge",
                    recorded_at=recorded_at,
                )
            )
            inputs = campaign_package_inputs_from_postgres(
                connection,
                campaign_id,
                producing_versions={"labbridge": "0.1.0", "campaign_package": "1"},
                limitations=[
                    "Fixture-backed replay does not establish physical-system performance."
                ],
            )

        assert not any(row["relation_id"] == str(relation_id) for row in inputs.relations)
    finally:
        with engine.begin() as connection:
            connection.execute(
                record_relations.delete().where(record_relations.c.relation_id == relation_id)
            )


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [("missing", "object_missing"), ("modified", "object_sha256_mismatch")],
)
def test_campaign_package_full_verification_detects_real_minio_invalidation(
    completed_campaign: tuple[Engine, uuid.UUID],
    object_store: S3ObjectStore,
    mutation: str,
    expected_code: str,
) -> None:
    engine, campaign_id = completed_campaign
    with engine.begin() as connection:
        inputs = campaign_package_inputs_from_postgres(
            connection,
            campaign_id,
            producing_versions={"labbridge": "0.1.0", "campaign_package": "1"},
            limitations=["Fixture-backed replay does not establish physical-system performance."],
        )
    package = build_campaign_experiment_package(inputs)
    entry = inputs.object_inventory[0]
    key = str(entry["key"])
    original = object_store.get(key)
    settings = ObjectStoreSettings()
    client = boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key,
        aws_secret_access_key=settings.secret_key,
        config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 1}),
        region_name=settings.region,
    )
    try:
        if mutation == "missing":
            client.delete_object(Bucket=object_store.bucket, Key=key)
        else:
            tampered = bytes([original[0] ^ 1]) + original[1:]
            client.put_object(Bucket=object_store.bucket, Key=key, Body=tampered)

        with pytest.raises(ExperimentPackageVerificationError) as caught:
            verify_experiment_package(package.archive_bytes, object_store=object_store)

        assert caught.value.code == expected_code
        with engine.begin() as connection:
            state = connection.execute(
                select(storage_objects.c.state).where(
                    storage_objects.c.object_uri == entry["object_uri"]
                )
            ).scalar_one()
        assert state == "committed"
    finally:
        client.put_object(Bucket=object_store.bucket, Key=key, Body=original)

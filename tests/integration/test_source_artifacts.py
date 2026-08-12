"""Source capture across real PostgreSQL and the configured S3-compatible object store."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, delete, select

from labbridge.application.source_intake import (
    IntakeConflictError,
    IntakeSource,
    SourceArtifactService,
    SourceIntegrityError,
)
from labbridge.infrastructure.objectstore import S3ObjectStore
from labbridge.infrastructure.persistence.source_artifacts import PostgresSourceArtifactRepository
from labbridge.infrastructure.persistence.tables import (
    idempotency_keys,
    source_artifacts,
    storage_objects,
)
from labbridge.runtime.reconciliation import reconcile

pytestmark = pytest.mark.integration


@pytest.fixture
def source_service(
    migrated: Engine, object_store: S3ObjectStore
) -> Iterator[tuple[SourceArtifactService, str]]:
    marker = uuid.uuid4().hex
    repository = PostgresSourceArtifactRepository(migrated)
    service = SourceArtifactService(
        repository,
        object_store,
        clock=lambda: datetime(2026, 8, 12, tzinfo=UTC),
    )
    yield service, marker
    with migrated.begin() as connection:
        rows = list(
            connection.execute(
                source_artifacts.select().where(source_artifacts.c.filename.like(f"%{marker}%"))
            ).mappings()
        )
        artifact_ids = [row["source_artifact_id"] for row in rows]
        object_uris = [row["object_uri"] for row in rows]
        connection.execute(
            delete(idempotency_keys).where(
                idempotency_keys.c.idempotency_key.like(f"source.intake:%{marker}%")
            )
        )
        if artifact_ids:
            connection.execute(
                delete(source_artifacts).where(
                    source_artifacts.c.source_artifact_id.in_(artifact_ids)
                )
            )
        if object_uris:
            connection.execute(
                delete(storage_objects).where(storage_objects.c.object_uri.in_(object_uris))
            )


def _command(marker: str, data: bytes) -> IntakeSource:
    return IntakeSource(
        intake_id=f"{marker}:capture",
        data=data,
        filename=f"synthetic-replay-cv-{marker}.csv",
        media_type="text/csv",
        data_origin="synthetic",
        execution_mode="replay",
    )


def test_exact_bytes_survive_source_intake(
    source_service: tuple[SourceArtifactService, str],
) -> None:
    service, marker = source_service
    payload = f"opaque,{marker}\r\n".encode()

    result = service.intake(_command(marker, payload))
    retrieved = service.retrieve(result.artifact.source_artifact_id)

    assert retrieved.data == payload
    assert retrieved.artifact.state == "committed"
    assert retrieved.artifact.byte_size == len(payload)
    assert retrieved.artifact.sha256 == hashlib.sha256(payload).hexdigest()


def test_same_intake_is_idempotent_and_changed_bytes_conflict(
    source_service: tuple[SourceArtifactService, str],
) -> None:
    service, marker = source_service
    command = _command(marker, marker.encode())

    first = service.intake(command)
    second = service.intake(command)

    assert second.replayed
    assert second.artifact.source_artifact_id == first.artifact.source_artifact_id
    with pytest.raises(IntakeConflictError):
        service.intake(command.model_copy(update={"data": command.data + b"changed"}))


def test_global_reconciliation_recognises_committed_source_evidence(
    source_service: tuple[SourceArtifactService, str],
    migrated: Engine,
    object_store: S3ObjectStore,
) -> None:
    service, marker = source_service
    result = service.intake(_command(marker, f"retained,{marker}".encode()))

    with migrated.begin() as connection:
        report = reconcile(connection, object_store)
        stored = connection.execute(
            select(storage_objects).where(
                storage_objects.c.object_uri == result.artifact.object_uri
            )
        ).one()

    classification = {item.object_uri: item.classification for item in report.classified}
    assert classification[result.artifact.object_uri] == "accepted_evidence"
    assert stored.classification == "accepted_evidence"
    assert stored.state == "committed"


def test_reconciliation_commits_an_object_uploaded_after_pending_metadata(
    source_service: tuple[SourceArtifactService, str],
    migrated: Engine,
    object_store: S3ObjectStore,
) -> None:
    service, marker = source_service
    command = _command(marker, f"pending,{marker}".encode())
    pending = service.describe(command)
    repository = PostgresSourceArtifactRepository(migrated)
    repository.reserve(
        intake_id=command.intake_id,
        request_hash=service.request_hash(pending),
        pending=pending,
    )
    key = pending.object_uri.removeprefix(f"s3://{object_store.bucket}/")
    object_store.put_and_verify(key, command.data, media_type=command.media_type)

    report = service.reconcile()

    assert any(
        item.source_artifact_id == pending.source_artifact_id and item.classification == "committed"
        for item in report
    )


def test_tampered_minio_bytes_are_detected_and_the_source_is_quarantined(
    source_service: tuple[SourceArtifactService, str],
    object_store: S3ObjectStore,
) -> None:
    service, marker = source_service
    command = _command(marker, f"tamper,{marker}".encode())
    result = service.intake(command)
    key = result.artifact.object_uri.removeprefix(f"s3://{object_store.bucket}/")
    object_store._client.put_object(
        Bucket=object_store.bucket,
        Key=key,
        Body=b"tampered after commit",
        ContentType=command.media_type,
    )

    with pytest.raises(SourceIntegrityError):
        service.retrieve(result.artifact.source_artifact_id)

    assert service.lookup(result.artifact.source_artifact_id).state == "quarantined"

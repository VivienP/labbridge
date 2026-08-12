"""Source intake through framework-independent application ports."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from labbridge.application.source_intake import (
    IntakeConflictError,
    IntakeSource,
    SourceArtifactRepository,
    SourceArtifactService,
    SourceIntegrityError,
    SourceNotReadyError,
)
from labbridge.domain.source_artifacts import SourceArtifact

NOW = datetime(2026, 8, 12, tzinfo=UTC)
PAYLOAD = b"cycle,current\r\n0,1.0\r\n"


@dataclass(frozen=True)
class Receipt:
    uri: str
    byte_size: int
    sha256: str


class MemoryStore:
    bucket = "test-sources"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_count = 0

    def put_and_verify(self, key: str, data: bytes, *, media_type: str) -> Receipt:
        del media_type
        self.put_count += 1
        self.objects[key] = data
        return Receipt(
            uri=f"s3://{self.bucket}/{key}",
            byte_size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def get(self, key: str) -> bytes:
        return self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects


class MemoryRepository(SourceArtifactRepository):
    def __init__(self) -> None:
        self.artifacts: dict[str, SourceArtifact] = {}
        self.intakes: dict[str, tuple[str, str]] = {}

    def reserve(
        self, *, intake_id: str, request_hash: str, pending: SourceArtifact
    ) -> tuple[SourceArtifact, bool]:
        existing = self.intakes.get(intake_id)
        if existing is not None:
            stored_hash, artifact_id = existing
            if stored_hash != request_hash:
                raise IntakeConflictError(intake_id)
            return self.artifacts[artifact_id], True
        self.intakes[intake_id] = (request_hash, pending.source_artifact_id)
        artifact = self.artifacts.setdefault(pending.source_artifact_id, pending)
        return artifact, artifact is not pending

    def get(self, source_artifact_id: str) -> SourceArtifact | None:
        return self.artifacts.get(source_artifact_id)

    def commit(self, source_artifact_id: str, *, committed_at: datetime) -> SourceArtifact:
        artifact = self.artifacts[source_artifact_id].model_copy(
            update={"state": "committed", "committed_at": committed_at}
        )
        self.artifacts[source_artifact_id] = artifact
        return artifact

    def quarantine(self, source_artifact_id: str, *, reason: str) -> SourceArtifact:
        artifact = self.artifacts[source_artifact_id].model_copy(
            update={"state": "quarantined", "quarantine_reason": reason}
        )
        self.artifacts[source_artifact_id] = artifact
        return artifact

    def pending(self) -> tuple[SourceArtifact, ...]:
        return tuple(item for item in self.artifacts.values() if item.state == "pending")


def _command(*, data: bytes = PAYLOAD, intake_id: str = "intake-1") -> IntakeSource:
    return IntakeSource(
        intake_id=intake_id,
        data=data,
        filename="synthetic-replay-cv-opaque.csv",
        media_type="text/csv",
        data_origin="synthetic",
        execution_mode="replay",
    )


def _service() -> tuple[SourceArtifactService, MemoryRepository, MemoryStore]:
    repository = MemoryRepository()
    store = MemoryStore()
    return SourceArtifactService(repository, store, clock=lambda: NOW), repository, store


def test_intake_retains_exact_bytes_and_explicit_metadata() -> None:
    service, _, _ = _service()

    result = service.intake(_command())
    retrieved = service.retrieve(result.artifact.source_artifact_id)

    assert retrieved.data == PAYLOAD
    assert retrieved.artifact.byte_size == len(PAYLOAD)
    assert retrieved.artifact.sha256 == hashlib.sha256(PAYLOAD).hexdigest()
    assert retrieved.artifact.filename == "synthetic-replay-cv-opaque.csv"
    assert retrieved.artifact.data_origin == "synthetic"
    assert retrieved.artifact.execution_mode == "replay"
    assert retrieved.artifact.state == "committed"


def test_identical_retry_returns_the_same_artifact_without_a_second_upload() -> None:
    service, _, store = _service()

    first = service.intake(_command())
    second = service.intake(_command())

    assert second.artifact.source_artifact_id == first.artifact.source_artifact_id
    assert second.replayed
    assert store.put_count == 1


def test_reused_intake_identity_with_changed_bytes_is_a_stable_conflict() -> None:
    service, _, _ = _service()
    service.intake(_command())

    with pytest.raises(IntakeConflictError) as caught:
        service.intake(_command(data=PAYLOAD + b"changed"))

    assert caught.value.code == "source_intake_conflict"


def test_retrieval_quarantines_bytes_that_no_longer_match() -> None:
    service, repository, store = _service()
    result = service.intake(_command())
    key = result.artifact.object_uri.removeprefix(f"s3://{store.bucket}/")
    store.objects[key] = b"tampered"

    with pytest.raises(SourceIntegrityError) as caught:
        service.retrieve(result.artifact.source_artifact_id)

    assert caught.value.code == "source_integrity_mismatch"
    assert repository.artifacts[result.artifact.source_artifact_id].state == "quarantined"


def test_pending_source_is_not_returned_as_verified_content() -> None:
    service, repository, _ = _service()
    command = _command()
    digest = hashlib.sha256(command.data).hexdigest()
    pending = service.describe(command)
    repository.reserve(intake_id=command.intake_id, request_hash=digest, pending=pending)

    with pytest.raises(SourceNotReadyError):
        service.retrieve(pending.source_artifact_id)


def test_reconciliation_commits_bytes_left_after_the_database_boundary() -> None:
    service, repository, store = _service()
    pending = service.describe(_command())
    repository.artifacts[pending.source_artifact_id] = pending
    key = pending.object_uri.removeprefix(f"s3://{store.bucket}/")
    store.objects[key] = PAYLOAD

    report = service.reconcile()

    assert report[0].classification == "committed"
    assert repository.artifacts[pending.source_artifact_id].state == "committed"


def test_reconciliation_quarantines_missing_pending_bytes_without_deleting_metadata() -> None:
    service, repository, _ = _service()
    pending = service.describe(_command())
    repository.artifacts[pending.source_artifact_id] = pending

    report = service.reconcile()

    assert report[0].classification == "quarantined"
    assert repository.artifacts[pending.source_artifact_id].quarantine_reason == "object_missing"


def test_reconciliation_quarantines_mismatched_pending_bytes_without_deleting_them() -> None:
    service, repository, store = _service()
    pending = service.describe(_command())
    repository.artifacts[pending.source_artifact_id] = pending
    key = pending.object_uri.removeprefix(f"s3://{store.bucket}/")
    store.objects[key] = b"mismatched pending evidence"

    report = service.reconcile()

    assert report[0].classification == "quarantined"
    assert report[0].reason == "object_readback_mismatch"
    assert store.objects[key] == b"mismatched pending evidence"

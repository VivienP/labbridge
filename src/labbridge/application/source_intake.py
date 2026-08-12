"""Retain and verify opaque source bytes without assigning scientific semantics."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field

from labbridge.domain.canonical import canonical_bytes
from labbridge.domain.identity import DataOrigin, ExecutionMode
from labbridge.domain.source_artifacts import SourceArtifact, source_artifact_id


class SourceIntakeError(Exception):
    code: ClassVar[str] = "source_intake_error"


class IntakeConflictError(SourceIntakeError):
    code = "source_intake_conflict"

    def __init__(self, intake_id: str) -> None:
        self.intake_id = intake_id
        super().__init__(f"intake identity `{intake_id}` was already used for different content")


class SourceNotFoundError(SourceIntakeError):
    code = "source_artifact_not_found"

    def __init__(self, source_artifact_id: str) -> None:
        self.source_artifact_id = source_artifact_id
        super().__init__(f"source artifact `{source_artifact_id}` does not exist")


class SourceNotReadyError(SourceIntakeError):
    code = "source_artifact_not_ready"

    def __init__(self, artifact: SourceArtifact) -> None:
        self.artifact = artifact
        super().__init__(
            f"source artifact `{artifact.source_artifact_id}` is {artifact.state}, not committed"
        )


class SourceIntegrityError(SourceIntakeError):
    code = "source_integrity_mismatch"

    def __init__(self, artifact: SourceArtifact, *, actual_sha256: str, actual_size: int) -> None:
        self.artifact = artifact
        self.actual_sha256 = actual_sha256
        self.actual_size = actual_size
        super().__init__(
            f"source artifact `{artifact.source_artifact_id}` expected "
            f"sha256:{artifact.sha256} and {artifact.byte_size} bytes, got "
            f"sha256:{actual_sha256} and {actual_size} bytes"
        )


class IntakeSource(BaseModel):
    """Opaque bytes plus declarations supplied by the intake adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    intake_id: str = Field(min_length=1)
    data: bytes
    filename: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    data_origin: DataOrigin
    execution_mode: ExecutionMode


class StoredSource(Protocol):
    @property
    def byte_size(self) -> int: ...

    @property
    def sha256(self) -> str: ...


class SourceObjectStore(Protocol):
    bucket: str

    def put_and_verify(self, key: str, data: bytes, *, media_type: str) -> StoredSource: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...


class SourceArtifactRepository(Protocol):
    """Durable metadata operations; each method owns its database transaction."""

    def reserve(
        self, *, intake_id: str, request_hash: str, pending: SourceArtifact
    ) -> tuple[SourceArtifact, bool]: ...

    def get(self, source_artifact_id: str) -> SourceArtifact | None: ...

    def commit(self, source_artifact_id: str, *, committed_at: datetime) -> SourceArtifact: ...

    def quarantine(self, source_artifact_id: str, *, reason: str) -> SourceArtifact: ...

    def pending(self) -> tuple[SourceArtifact, ...]: ...


@dataclass(frozen=True)
class IntakeResult:
    artifact: SourceArtifact
    replayed: bool


@dataclass(frozen=True)
class RetrievedSource:
    artifact: SourceArtifact
    data: bytes


@dataclass(frozen=True)
class ReconciliationResult:
    source_artifact_id: str
    classification: str
    reason: str | None = None


class SourceArtifactService:
    """The one source-capture use case shared by CLI, HTTP, and reproduction adapters."""

    def __init__(
        self,
        repository: SourceArtifactRepository,
        object_store: SourceObjectStore,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._object_store = object_store
        self._clock = clock

    def describe(self, command: IntakeSource) -> SourceArtifact:
        digest = hashlib.sha256(command.data).hexdigest()
        identity = source_artifact_id(
            sha256=digest,
            byte_size=len(command.data),
            media_type=command.media_type,
        )
        key = self._object_key(digest)
        return SourceArtifact(
            source_artifact_id=identity,
            filename=command.filename,
            media_type=command.media_type,
            byte_size=len(command.data),
            sha256=digest,
            data_origin=command.data_origin,
            execution_mode=command.execution_mode,
            state="pending",
            object_uri=f"s3://{self._object_store.bucket}/{key}",
            created_at=self._clock(),
        )

    def intake(self, command: IntakeSource) -> IntakeResult:
        pending = self.describe(command)
        request_hash = self.request_hash(pending)
        artifact, replayed = self._repository.reserve(
            intake_id=command.intake_id,
            request_hash=request_hash,
            pending=pending,
        )
        if artifact.state == "committed":
            return IntakeResult(artifact=artifact, replayed=True)
        if artifact.state == "quarantined":
            raise SourceNotReadyError(artifact)

        key = self._key_from_uri(artifact.object_uri)
        stored = self._object_store.put_and_verify(key, command.data, media_type=command.media_type)
        if stored.sha256 != artifact.sha256 or stored.byte_size != artifact.byte_size:
            quarantined = self._repository.quarantine(
                artifact.source_artifact_id, reason="object_readback_mismatch"
            )
            raise SourceIntegrityError(
                quarantined,
                actual_sha256=stored.sha256,
                actual_size=stored.byte_size,
            )
        committed = self._repository.commit(artifact.source_artifact_id, committed_at=self._clock())
        return IntakeResult(artifact=committed, replayed=replayed)

    @staticmethod
    def request_hash(artifact: SourceArtifact) -> str:
        """Hash the declarations that must remain stable for one intake identity."""
        return hashlib.sha256(
            canonical_bytes(
                {
                    "source_artifact_id": artifact.source_artifact_id,
                    "data_origin": artifact.data_origin,
                    "execution_mode": artifact.execution_mode,
                }
            )
        ).hexdigest()

    def lookup(self, source_artifact_id: str) -> SourceArtifact:
        artifact = self._repository.get(source_artifact_id)
        if artifact is None:
            raise SourceNotFoundError(source_artifact_id)
        return artifact

    def retrieve(self, source_artifact_id: str) -> RetrievedSource:
        artifact = self.lookup(source_artifact_id)
        if artifact.state != "committed":
            raise SourceNotReadyError(artifact)
        data = self._object_store.get(self._key_from_uri(artifact.object_uri))
        actual_sha256 = hashlib.sha256(data).hexdigest()
        if actual_sha256 != artifact.sha256 or len(data) != artifact.byte_size:
            quarantined = self._repository.quarantine(
                source_artifact_id, reason="object_readback_mismatch"
            )
            raise SourceIntegrityError(
                quarantined,
                actual_sha256=actual_sha256,
                actual_size=len(data),
            )
        return RetrievedSource(artifact=artifact, data=data)

    def verify(self, source_artifact_id: str) -> SourceArtifact:
        return self.retrieve(source_artifact_id).artifact

    def reconcile(self) -> tuple[ReconciliationResult, ...]:
        results: list[ReconciliationResult] = []
        for artifact in self._repository.pending():
            key = self._key_from_uri(artifact.object_uri)
            if not self._object_store.exists(key):
                self._repository.quarantine(artifact.source_artifact_id, reason="object_missing")
                results.append(
                    ReconciliationResult(
                        artifact.source_artifact_id, "quarantined", "object_missing"
                    )
                )
                continue
            data = self._object_store.get(key)
            actual_sha256 = hashlib.sha256(data).hexdigest()
            if actual_sha256 != artifact.sha256 or len(data) != artifact.byte_size:
                self._repository.quarantine(
                    artifact.source_artifact_id, reason="object_readback_mismatch"
                )
                results.append(
                    ReconciliationResult(
                        artifact.source_artifact_id,
                        "quarantined",
                        "object_readback_mismatch",
                    )
                )
                continue
            self._repository.commit(artifact.source_artifact_id, committed_at=self._clock())
            results.append(ReconciliationResult(artifact.source_artifact_id, "committed"))
        return tuple(results)

    @staticmethod
    def _object_key(sha256: str) -> str:
        return f"source-artifacts/sha256/{sha256}"

    def _key_from_uri(self, uri: str) -> str:
        prefix = f"s3://{self._object_store.bucket}/"
        if not uri.startswith(prefix):
            raise ValueError(f"object URI `{uri}` is outside bucket `{self._object_store.bucket}`")
        return uri.removeprefix(prefix)


__all__ = [
    "IntakeConflictError",
    "IntakeResult",
    "IntakeSource",
    "ReconciliationResult",
    "RetrievedSource",
    "SourceArtifactRepository",
    "SourceArtifactService",
    "SourceIntakeError",
    "SourceIntegrityError",
    "SourceNotFoundError",
    "SourceNotReadyError",
]

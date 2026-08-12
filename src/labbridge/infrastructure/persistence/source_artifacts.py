"""PostgreSQL metadata adapter for the source-artifact application port."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Engine, RowMapping, func, select, update
from sqlalchemy.dialects.postgresql import insert

from labbridge.application.source_intake import IntakeConflictError
from labbridge.domain.source_artifacts import SourceArtifact

from .tables import idempotency_keys, source_artifacts, storage_objects

_SCOPE = "source.intake"


def _idempotency_key(intake_id: str) -> str:
    return f"{_SCOPE}:{intake_id}"


def _artifact(row: RowMapping) -> SourceArtifact:
    return SourceArtifact(
        source_artifact_id=row["source_artifact_id"],
        filename=row["filename"],
        media_type=row["media_type"],
        byte_size=row["byte_size"],
        sha256=row["sha256"],
        data_origin=row["data_origin"],
        execution_mode=row["execution_mode"],
        state=row["state"],
        object_uri=row["object_uri"],
        created_at=row["created_at"],
        committed_at=row["committed_at"],
        quarantine_reason=row["quarantine_reason"],
    )


def _values(artifact: SourceArtifact) -> dict[str, Any]:
    return artifact.model_dump(mode="python")


class PostgresSourceArtifactRepository:
    """Each method opens one explicit transaction around its metadata effects."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def reserve(
        self, *, intake_id: str, request_hash: str, pending: SourceArtifact
    ) -> tuple[SourceArtifact, bool]:
        key = _idempotency_key(intake_id)
        with self._engine.begin() as connection:
            existing = (
                connection.execute(
                    select(idempotency_keys.c.request_hash, idempotency_keys.c.response).where(
                        idempotency_keys.c.scope == _SCOPE,
                        idempotency_keys.c.idempotency_key == key,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise IntakeConflictError(intake_id)
                response = existing["response"] or {}
                artifact_id = str(response["source_artifact_id"])
                row = (
                    connection.execute(
                        select(source_artifacts).where(
                            source_artifacts.c.source_artifact_id == artifact_id
                        )
                    )
                    .mappings()
                    .one()
                )
                return _artifact(row), True

            bucket = pending.object_uri.removeprefix("s3://").split("/", 1)[0]
            object_key = pending.object_uri.removeprefix(f"s3://{bucket}/")
            connection.execute(
                insert(storage_objects)
                .values(
                    object_uri=pending.object_uri,
                    bucket=bucket,
                    object_key=object_key,
                    state="pending",
                    created_at=pending.created_at,
                )
                .on_conflict_do_nothing(index_elements=[storage_objects.c.object_uri])
            )
            source_result = connection.execute(
                insert(source_artifacts)
                .values(**_values(pending))
                .on_conflict_do_nothing(index_elements=[source_artifacts.c.source_artifact_id])
            )
            row = (
                connection.execute(
                    select(source_artifacts).where(
                        source_artifacts.c.source_artifact_id == pending.source_artifact_id
                    )
                )
                .mappings()
                .one()
            )
            retained = _artifact(row)
            if (
                retained.data_origin != pending.data_origin
                or retained.execution_mode != pending.execution_mode
            ):
                raise IntakeConflictError(intake_id)

            idempotency_result = connection.execute(
                insert(idempotency_keys)
                .values(
                    idempotency_key=key,
                    scope=_SCOPE,
                    request_hash=request_hash,
                    response={"source_artifact_id": pending.source_artifact_id},
                    created_at=pending.created_at,
                )
                .on_conflict_do_nothing(
                    index_elements=[idempotency_keys.c.scope, idempotency_keys.c.idempotency_key]
                )
            )
            if idempotency_result.rowcount == 0:
                winner = connection.execute(
                    select(idempotency_keys.c.request_hash).where(
                        idempotency_keys.c.scope == _SCOPE,
                        idempotency_keys.c.idempotency_key == key,
                    )
                ).scalar_one()
                if winner != request_hash:
                    raise IntakeConflictError(intake_id)
            return retained, source_result.rowcount == 0 or idempotency_result.rowcount == 0

    def get(self, source_artifact_id: str) -> SourceArtifact | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(source_artifacts).where(
                        source_artifacts.c.source_artifact_id == source_artifact_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _artifact(row) if row is not None else None

    def commit(self, source_artifact_id: str, *, committed_at: datetime) -> SourceArtifact:
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    select(source_artifacts).where(
                        source_artifacts.c.source_artifact_id == source_artifact_id
                    )
                )
                .mappings()
                .one()
            )
            artifact = _artifact(row)
            connection.execute(
                update(storage_objects)
                .where(storage_objects.c.object_uri == artifact.object_uri)
                .values(
                    state="committed",
                    byte_size=artifact.byte_size,
                    sha256=artifact.sha256,
                    committed_at=committed_at,
                    classification="accepted_evidence",
                    classification_reason=(
                        "referenced by a committed source artifact, and read-back size and SHA-256 "
                        "match the retained bytes"
                    ),
                    reconciled_at=committed_at,
                )
            )
            connection.execute(
                update(source_artifacts)
                .where(source_artifacts.c.source_artifact_id == source_artifact_id)
                .values(state="committed", committed_at=committed_at)
            )
        committed = self.get(source_artifact_id)
        assert committed is not None
        return committed

    def quarantine(self, source_artifact_id: str, *, reason: str) -> SourceArtifact:
        with self._engine.begin() as connection:
            row = connection.execute(
                select(source_artifacts.c.object_uri).where(
                    source_artifacts.c.source_artifact_id == source_artifact_id
                )
            ).one()
            connection.execute(
                update(source_artifacts)
                .where(source_artifacts.c.source_artifact_id == source_artifact_id)
                .values(state="quarantined", quarantine_reason=reason, committed_at=None)
            )
            if reason == "object_readback_mismatch":
                connection.execute(
                    update(storage_objects)
                    .where(storage_objects.c.object_uri == row.object_uri)
                    .values(
                        state="orphaned",
                        classification="quarantined",
                        classification_reason=(
                            "source-artifact read-back size or SHA-256 differs from its committed "
                            "metadata"
                        ),
                        reconciled_at=func.now(),
                    )
                )
        quarantined = self.get(source_artifact_id)
        assert quarantined is not None
        return quarantined

    def pending(self) -> tuple[SourceArtifact, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(source_artifacts)
                .where(source_artifacts.c.state == "pending")
                .order_by(source_artifacts.c.created_at, source_artifacts.c.source_artifact_id)
            ).mappings()
            return tuple(_artifact(row) for row in rows)


__all__ = ["PostgresSourceArtifactRepository"]

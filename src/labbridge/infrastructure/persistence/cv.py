"""PostgreSQL metadata and S3-compatible object persistence for normalised CV records."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import Connection, Engine, select, update
from sqlalchemy.dialects.postgresql import insert

from labbridge.application.cv_ingestion import (
    CVIdempotencyConflictError,
    NormalisedObservationIntegrityError,
    ParserRecordIntegrityError,
)
from labbridge.domain.canonical import canonical_bytes
from labbridge.domain.cv import CVImportProfile, import_profile_id
from labbridge.domain.cv_observations import NormalisationResult
from labbridge.domain.parser_diagnostics import ParserRecord
from labbridge.evidence.manifest import canonical_json, digest
from labbridge.infrastructure.objectstore import ObjectStore

from .tables import (
    cv_parser_records,
    cv_structural_findings,
    cv_transformation_records,
    idempotency_keys,
    import_profiles,
    normalised_cv_observations,
    storage_objects,
)

_PROFILE_SCOPE = "cv.profile.create"
_NORMALISE_SCOPE = "cv.normalise"


class PostgresCVRecordRepository:
    def __init__(
        self, engine: Engine, object_store: ObjectStore, *, clock: Callable[[], datetime]
    ) -> None:
        self._engine = engine
        self._store = object_store
        self._clock = clock

    def _reserve_idempotency(
        self,
        connection: Connection,
        *,
        scope: str,
        idempotency_key: str | None,
        request_hash: str,
        response: dict[str, str],
    ) -> bool:
        if idempotency_key is None:
            return False
        inserted = connection.execute(
            insert(idempotency_keys)
            .values(
                scope=scope,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
                created_at=self._clock(),
            )
            .on_conflict_do_nothing(
                index_elements=[idempotency_keys.c.scope, idempotency_keys.c.idempotency_key]
            )
        )
        if inserted.rowcount != 0:
            return False
        existing_hash = connection.execute(
            select(idempotency_keys.c.request_hash).where(
                idempotency_keys.c.scope == scope,
                idempotency_keys.c.idempotency_key == idempotency_key,
            )
        ).scalar_one()
        if existing_hash != request_hash:
            raise CVIdempotencyConflictError(idempotency_key)
        return True

    def _reserve_normalised_object(
        self,
        *,
        key: str,
        byte_size: int,
        sha256: str,
        created_at: datetime,
    ) -> str:
        object_uri = f"s3://{self._store.bucket}/{key}"
        with self._engine.begin() as connection:
            connection.execute(
                insert(storage_objects)
                .values(
                    object_uri=object_uri,
                    bucket=self._store.bucket,
                    object_key=key,
                    byte_size=byte_size,
                    sha256=sha256,
                    media_type="application/json",
                    state="pending",
                    created_at=created_at,
                )
                .on_conflict_do_nothing(index_elements=[storage_objects.c.object_uri])
            )
            row = connection.execute(
                select(
                    storage_objects.c.bucket,
                    storage_objects.c.object_key,
                    storage_objects.c.byte_size,
                    storage_objects.c.sha256,
                    storage_objects.c.state,
                    storage_objects.c.classification,
                ).where(storage_objects.c.object_uri == object_uri)
            ).one()
            if (
                row.bucket != self._store.bucket
                or row.object_key != key
                or row.byte_size not in {None, byte_size}
                or row.sha256 not in {None, sha256}
                or row.state == "orphaned"
                or row.classification == "quarantined"
            ):
                raise NormalisedObservationIntegrityError(object_uri)
        return object_uri

    def put_profile(
        self, item: CVImportProfile, *, idempotency_key: str | None = None
    ) -> tuple[str, bool]:
        profile_id = import_profile_id(item)
        request_hash = digest(canonical_bytes({"profile_id": profile_id}))
        with self._engine.begin() as connection:
            idempotent_replay = self._reserve_idempotency(
                connection,
                scope=_PROFILE_SCOPE,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response={"profile_id": profile_id},
            )
            result = connection.execute(
                insert(import_profiles)
                .values(
                    profile_id=profile_id,
                    schema_version=item.schema_version,
                    technique=item.technique,
                    body=item.model_dump(mode="json"),
                    created_at=self._clock(),
                )
                .on_conflict_do_nothing(index_elements=[import_profiles.c.profile_id])
            )
        return profile_id, idempotent_replay or result.rowcount == 0

    def get_profile(self, profile_id: str) -> CVImportProfile | None:
        with self._engine.connect() as connection:
            body = connection.execute(
                select(import_profiles.c.body).where(import_profiles.c.profile_id == profile_id)
            ).scalar_one_or_none()
        return CVImportProfile.model_validate(body) if body is not None else None

    def _insert_parser_record(
        self,
        connection: Connection,
        record: ParserRecord,
        *,
        observation_id: str | None,
    ) -> bool:
        if (record.status == "accepted") != (observation_id is not None):
            raise ParserRecordIntegrityError(record.parser_record_id)
        result = connection.execute(
            insert(cv_parser_records)
            .values(
                parser_record_id=record.parser_record_id,
                source_artifact_id=record.source_artifact_id,
                profile_id=record.import_profile_id,
                observation_id=observation_id,
                source_format=record.source_format,
                parser_version=record.parser_version,
                status=record.status,
                body=record.model_dump(mode="json"),
                created_at=self._clock(),
            )
            .on_conflict_do_nothing(index_elements=[cv_parser_records.c.parser_record_id])
        )
        if result.rowcount != 0:
            return False
        existing = connection.execute(
            select(cv_parser_records.c.observation_id, cv_parser_records.c.body).where(
                cv_parser_records.c.parser_record_id == record.parser_record_id
            )
        ).one()
        if existing.observation_id != observation_id or existing.body != record.model_dump(
            mode="json"
        ):
            raise ParserRecordIntegrityError(record.parser_record_id)
        return True

    def put_parser_record(self, record: ParserRecord) -> bool:
        if record.status != "rejected":
            raise ParserRecordIntegrityError(record.parser_record_id)
        with self._engine.begin() as connection:
            return self._insert_parser_record(connection, record, observation_id=None)

    def get_parser_record(self, parser_record_id: str) -> ParserRecord | None:
        with self._engine.connect() as connection:
            body = connection.execute(
                select(cv_parser_records.c.body).where(
                    cv_parser_records.c.parser_record_id == parser_record_id
                )
            ).scalar_one_or_none()
        if body is None:
            return None
        try:
            record = ParserRecord.model_validate(body)
        except ValueError as error:
            raise ParserRecordIntegrityError(parser_record_id) from error
        if record.parser_record_id != parser_record_id:
            raise ParserRecordIntegrityError(parser_record_id)
        return record

    def put_normalisation(
        self, result: NormalisationResult, *, idempotency_key: str | None = None
    ) -> bool:
        observation = result.observation
        request_hash = digest(
            canonical_bytes(
                {
                    "source_artifact_id": observation.source_artifact_id,
                    "profile_id": observation.import_profile_id,
                    "observation_id": observation.observation_id,
                }
            )
        )
        payload = canonical_json(result.model_dump(mode="json"))
        payload_sha256 = digest(payload)
        key = f"normalised-cv/sha256/{payload_sha256}.json"
        now = self._clock()
        object_uri = self._reserve_normalised_object(
            key=key,
            byte_size=len(payload),
            sha256=payload_sha256,
            created_at=now,
        )
        stored = self._store.put_and_verify(key, payload, media_type="application/json")
        if (
            stored.uri != object_uri
            or stored.byte_size != len(payload)
            or stored.sha256 != payload_sha256
        ):
            raise NormalisedObservationIntegrityError(observation.observation_id)
        with self._engine.begin() as connection:
            idempotent_replay = self._reserve_idempotency(
                connection,
                scope=_NORMALISE_SCOPE,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response={"observation_id": observation.observation_id},
            )
            connection.execute(
                update(storage_objects)
                .where(storage_objects.c.object_uri == object_uri)
                .values(
                    byte_size=stored.byte_size,
                    sha256=stored.sha256,
                    media_type="application/json",
                    state="committed",
                    classification="accepted_evidence",
                    classification_reason=(
                        "referenced by a normalised CV observation after object read-back "
                        "verification"
                    ),
                    reconciled_at=now,
                    committed_at=now,
                )
            )
            exists = connection.execute(
                select(normalised_cv_observations.c.observation_id).where(
                    normalised_cv_observations.c.observation_id == observation.observation_id
                )
            ).scalar_one_or_none()
            if exists is not None:
                if result.parser_record is not None:
                    self._insert_parser_record(
                        connection,
                        result.parser_record,
                        observation_id=observation.observation_id,
                    )
                return True
            inserted = connection.execute(
                insert(normalised_cv_observations)
                .values(
                    observation_id=observation.observation_id,
                    source_artifact_id=observation.source_artifact_id,
                    profile_id=observation.import_profile_id,
                    schema_version=observation.schema_version,
                    parser_version=observation.parser_version,
                    normalisation_version=observation.normalisation_version,
                    data_origin=observation.data_origin,
                    execution_mode=observation.execution_mode,
                    environment_id=observation.environment_id,
                    row_count=observation.row_count,
                    object_uri=stored.uri,
                    byte_size=stored.byte_size,
                    sha256=stored.sha256,
                    created_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[normalised_cv_observations.c.observation_id]
                )
            )
            if inserted.rowcount == 0:
                return True
            for ordinal, record in enumerate(result.graph.records, start=1):
                connection.execute(
                    insert(cv_transformation_records).values(
                        transformation_id=record.transformation_id,
                        observation_id=observation.observation_id,
                        ordinal=ordinal,
                        record=record.model_dump(mode="json"),
                    )
                )
            for finding in result.findings:
                connection.execute(
                    insert(cv_structural_findings).values(
                        finding_id=finding.finding_id,
                        observation_id=observation.observation_id,
                        finding=finding.model_dump(mode="json"),
                    )
                )
            if result.parser_record is not None:
                self._insert_parser_record(
                    connection,
                    result.parser_record,
                    observation_id=observation.observation_id,
                )
        return idempotent_replay

    def get_normalisation(self, observation_id: str) -> NormalisationResult | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(
                    normalised_cv_observations.c.object_uri,
                    normalised_cv_observations.c.byte_size,
                    normalised_cv_observations.c.sha256,
                ).where(normalised_cv_observations.c.observation_id == observation_id)
            ).one_or_none()
        if row is None:
            return None
        prefix = f"s3://{self._store.bucket}/"
        if not row.object_uri.startswith(prefix):
            raise NormalisedObservationIntegrityError(observation_id)
        payload = self._store.get(row.object_uri.removeprefix(prefix))
        if len(payload) != row.byte_size or digest(payload) != row.sha256:
            raise NormalisedObservationIntegrityError(observation_id)
        result = NormalisationResult.model_validate_json(payload)
        if result.observation.observation_id != observation_id:
            raise NormalisedObservationIntegrityError(observation_id)
        return result


__all__ = ["PostgresCVRecordRepository"]

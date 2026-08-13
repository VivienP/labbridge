"""PostgreSQL and object-store persistence for normalised electrolysis records."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import Connection, Engine, select, update
from sqlalchemy.dialects.postgresql import insert

from labbridge.application.electrolysis_ingestion import (
    ElectrolysisIdempotencyConflictError,
    ElectrolysisObservationIntegrityError,
)
from labbridge.domain.canonical import canonical_bytes
from labbridge.domain.electrolysis import (
    ElectrolysisImportProfile,
    electrolysis_import_profile_id,
)
from labbridge.domain.electrolysis_observations import (
    ElectrolysisNormalisationResult,
    electrolysis_observation_id,
)
from labbridge.evidence.manifest import canonical_json, digest
from labbridge.infrastructure.objectstore import ObjectStore

from .tables import (
    electrolysis_auxiliary_results,
    electrolysis_import_profiles,
    electrolysis_structural_findings,
    electrolysis_transformation_records,
    idempotency_keys,
    normalised_electrolysis_observations,
    normalised_observations,
    storage_objects,
)

_PROFILE_SCOPE = "electrolysis.profile.create"
_NORMALISE_SCOPE = "electrolysis.normalise"


class PostgresElectrolysisRecordRepository:
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
            raise ElectrolysisIdempotencyConflictError(idempotency_key)
        return True

    def _reserve_object(
        self, *, key: str, byte_size: int, sha256: str, created_at: datetime
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
                    storage_objects.c.byte_size,
                    storage_objects.c.sha256,
                    storage_objects.c.state,
                    storage_objects.c.classification,
                ).where(storage_objects.c.object_uri == object_uri)
            ).one()
            if (
                row.byte_size not in {None, byte_size}
                or row.sha256 not in {None, sha256}
                or row.state == "orphaned"
                or row.classification == "quarantined"
            ):
                raise ElectrolysisObservationIntegrityError(object_uri)
        return object_uri

    def put_profile(
        self, item: ElectrolysisImportProfile, *, idempotency_key: str | None = None
    ) -> tuple[str, bool]:
        profile_id = electrolysis_import_profile_id(item)
        request_hash = digest(canonical_bytes({"profile_id": profile_id}))
        with self._engine.begin() as connection:
            replayed = self._reserve_idempotency(
                connection,
                scope=_PROFILE_SCOPE,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response={"profile_id": profile_id},
            )
            inserted = connection.execute(
                insert(electrolysis_import_profiles)
                .values(
                    profile_id=profile_id,
                    schema_version=item.schema_version,
                    technique=item.technique,
                    body=item.model_dump(mode="json"),
                    created_at=self._clock(),
                )
                .on_conflict_do_nothing(index_elements=[electrolysis_import_profiles.c.profile_id])
            )
        return profile_id, replayed or inserted.rowcount == 0

    def get_profile(self, profile_id: str) -> ElectrolysisImportProfile | None:
        with self._engine.connect() as connection:
            body = connection.execute(
                select(electrolysis_import_profiles.c.body).where(
                    electrolysis_import_profiles.c.profile_id == profile_id
                )
            ).scalar_one_or_none()
        return ElectrolysisImportProfile.model_validate(body) if body is not None else None

    def put_normalisation(
        self,
        result: ElectrolysisNormalisationResult,
        *,
        idempotency_key: str | None = None,
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
        key = f"normalised-electrolysis/sha256/{payload_sha256}.json"
        now = self._clock()
        object_uri = self._reserve_object(
            key=key, byte_size=len(payload), sha256=payload_sha256, created_at=now
        )
        stored = self._store.put_and_verify(key, payload, media_type="application/json")
        if (
            stored.uri != object_uri
            or stored.byte_size != len(payload)
            or stored.sha256 != payload_sha256
        ):
            raise ElectrolysisObservationIntegrityError(observation.observation_id)
        with self._engine.begin() as connection:
            replayed = self._reserve_idempotency(
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
                        "referenced by a normalised electrolysis observation after read-back"
                    ),
                    reconciled_at=now,
                    committed_at=now,
                )
            )
            exists = connection.execute(
                select(normalised_electrolysis_observations.c.observation_id).where(
                    normalised_electrolysis_observations.c.observation_id
                    == observation.observation_id
                )
            ).scalar_one_or_none()
            if exists is not None:
                return True
            connection.execute(
                insert(normalised_observations)
                .values(
                    observation_id=observation.observation_id,
                    technique=observation.technique,
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=[normalised_observations.c.observation_id])
            )
            inserted = connection.execute(
                insert(normalised_electrolysis_observations)
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
                    index_elements=[normalised_electrolysis_observations.c.observation_id]
                )
            )
            if inserted.rowcount == 0:
                return True
            for ordinal, record in enumerate(result.graph.records, start=1):
                connection.execute(
                    insert(electrolysis_transformation_records).values(
                        transformation_id=record.transformation_id,
                        observation_id=observation.observation_id,
                        ordinal=ordinal,
                        record=record.model_dump(mode="json"),
                    )
                )
            for finding in result.findings:
                connection.execute(
                    insert(electrolysis_structural_findings).values(
                        finding_id=finding.finding_id,
                        observation_id=observation.observation_id,
                        finding=finding.model_dump(mode="json"),
                    )
                )
            for auxiliary in observation.auxiliary_results:
                connection.execute(
                    insert(electrolysis_auxiliary_results).values(
                        result_id=auxiliary.result_id,
                        observation_id=observation.observation_id,
                        source_artifact_id=auxiliary.source_artifact_id,
                        method_name=auxiliary.method_name,
                        method_version=auxiliary.method_version,
                        body=auxiliary.model_dump(mode="json"),
                        created_at=now,
                    )
                )
        return replayed

    def get_normalisation(self, observation_id: str) -> ElectrolysisNormalisationResult | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(
                    normalised_electrolysis_observations.c.object_uri,
                    normalised_electrolysis_observations.c.byte_size,
                    normalised_electrolysis_observations.c.sha256,
                ).where(normalised_electrolysis_observations.c.observation_id == observation_id)
            ).one_or_none()
        if row is None:
            return None
        prefix = f"s3://{self._store.bucket}/"
        if not row.object_uri.startswith(prefix):
            raise ElectrolysisObservationIntegrityError(observation_id)
        payload = self._store.get(row.object_uri.removeprefix(prefix))
        if len(payload) != row.byte_size or digest(payload) != row.sha256:
            raise ElectrolysisObservationIntegrityError(observation_id)
        result = ElectrolysisNormalisationResult.model_validate_json(payload)
        if (
            result.observation.observation_id != observation_id
            or electrolysis_observation_id(result.observation) != observation_id
        ):
            raise ElectrolysisObservationIntegrityError(observation_id)
        return result


__all__ = ["PostgresElectrolysisRecordRepository"]

"""PostgreSQL and object-store persistence for Experiment Passport releases."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import Connection, Engine, select, update
from sqlalchemy.dialects.postgresql import insert

from labbridge.application.experiments import ExperimentIdempotencyConflictError
from labbridge.domain.experiments import (
    Experiment,
    ExperimentVersionConflictError,
    ValidationRun,
)
from labbridge.evidence.experiment_package import ExperimentPackage
from labbridge.evidence.manifest import digest
from labbridge.evidence.passport import (
    ExperimentPassport,
    render_passport_html,
    render_passport_json,
)
from labbridge.infrastructure.objectstore import ObjectStore

from .tables import (
    experiment_packages,
    experiment_passports,
    experiment_versions,
    experiments,
    idempotency_keys,
    metadata_assertions,
    storage_objects,
    validation_findings,
    validation_runs,
)

_CREATE_SCOPE = "experiment.create"
_ASSERTION_SCOPE = "experiment.assertion"
_VALIDATION_SCOPE = "experiment.validation"
_PASSPORT_SCOPE = "experiment.passport"
_PACKAGE_SCOPE = "experiment.package"


def _aggregate_scope(operation: str, experiment_id: str) -> str:
    return f"{operation}:{digest(experiment_id.encode('utf-8'))[:32]}"


def _response_int(value: object, field_name: str) -> int:
    if not isinstance(value, int):
        raise RuntimeError(f"experiment idempotency response field {field_name} is not an integer")
    return value


class PostgresExperimentRepository:
    """Append-only experiment snapshots and immutable released report objects."""

    def __init__(
        self,
        engine: Engine,
        object_store: ObjectStore,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._engine = engine
        self._store = object_store
        self._clock = clock

    def _reserve_idempotency(
        self,
        connection: Connection,
        *,
        scope: str,
        key: str,
        request_hash: str,
        response: dict[str, object],
    ) -> dict[str, object] | None:
        inserted_key = connection.execute(
            insert(idempotency_keys)
            .values(
                scope=scope,
                idempotency_key=key,
                request_hash=request_hash,
                response=response,
                created_at=self._clock(),
            )
            .on_conflict_do_nothing(
                index_elements=[idempotency_keys.c.scope, idempotency_keys.c.idempotency_key]
            )
            .returning(idempotency_keys.c.idempotency_key)
        ).scalar_one_or_none()
        if inserted_key is not None:
            return None
        row = connection.execute(
            select(idempotency_keys.c.request_hash, idempotency_keys.c.response).where(
                idempotency_keys.c.scope == scope,
                idempotency_keys.c.idempotency_key == key,
            )
        ).one()
        if row.request_hash != request_hash:
            raise ExperimentIdempotencyConflictError(key)
        if not isinstance(row.response, dict):
            raise RuntimeError("experiment idempotency response is not an object")
        return row.response

    @staticmethod
    def _lock_expected_version(
        connection: Connection, experiment_id: str, expected_version: int
    ) -> None:
        current = connection.execute(
            select(experiments.c.current_version)
            .where(experiments.c.experiment_id == experiment_id)
            .with_for_update()
        ).scalar_one_or_none()
        if current is None:
            raise RuntimeError("experiment does not exist")
        if current != expected_version:
            raise ExperimentVersionConflictError(expected_version, current)

    @staticmethod
    def _load_version(
        connection: Connection, experiment_id: str, version: int | None = None
    ) -> Experiment | None:
        if version is None:
            version = connection.execute(
                select(experiments.c.current_version).where(
                    experiments.c.experiment_id == experiment_id
                )
            ).scalar_one_or_none()
        if version is None:
            return None
        body = connection.execute(
            select(experiment_versions.c.body).where(
                experiment_versions.c.experiment_id == experiment_id,
                experiment_versions.c.version == version,
            )
        ).scalar_one_or_none()
        return Experiment.model_validate(body) if body is not None else None

    @staticmethod
    def _insert_assertions(
        connection: Connection, experiment: Experiment, *, created_version: int
    ) -> None:
        for assertion in experiment.assertions:
            connection.execute(
                insert(metadata_assertions)
                .values(
                    assertion_id=assertion.assertion_id,
                    experiment_id=experiment.experiment_id,
                    created_version=created_version,
                    schema_version=assertion.schema_version,
                    field_name=assertion.field_name,
                    origin=assertion.origin,
                    transformation=assertion.transformation,
                    requirement_class=assertion.requirement_class,
                    value_state=assertion.value.state,
                    supplements_assertion_id=assertion.supplements_assertion_id,
                    supersedes_assertion_id=assertion.supersedes_assertion_id,
                    body=assertion.model_dump(mode="json"),
                )
                .on_conflict_do_nothing(index_elements=[metadata_assertions.c.assertion_id])
            )

    def create(
        self, experiment: Experiment, *, idempotency_key: str, request_hash: str
    ) -> tuple[Experiment, bool]:
        now = self._clock()
        with self._engine.begin() as connection:
            replay = self._reserve_idempotency(
                connection,
                scope=_CREATE_SCOPE,
                key=idempotency_key,
                request_hash=request_hash,
                response={
                    "experiment_id": experiment.experiment_id,
                    "experiment_version": experiment.version,
                },
            )
            if replay is not None:
                stored = self._load_version(
                    connection,
                    str(replay["experiment_id"]),
                    _response_int(replay["experiment_version"], "experiment_version"),
                )
                if stored is None:
                    raise RuntimeError("experiment idempotency record names a missing version")
                return stored, True
            inserted_id = connection.execute(
                insert(experiments)
                .values(
                    experiment_id=experiment.experiment_id,
                    observation_id=experiment.observation_id,
                    schema_version=experiment.schema_version,
                    current_version=experiment.version,
                    technique=experiment.technique,
                    data_origin=experiment.data_origin,
                    execution_mode=experiment.execution_mode,
                    environment_id=experiment.environment_id,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=[experiments.c.experiment_id])
                .returning(experiments.c.experiment_id)
            ).scalar_one_or_none()
            if inserted_id is None:
                stored = self._load_version(connection, experiment.experiment_id)
                if stored is None:
                    raise RuntimeError("experiment identity exists without a version")
                return stored, True
            connection.execute(
                insert(experiment_versions).values(
                    experiment_id=experiment.experiment_id,
                    version=experiment.version,
                    supersedes_version=None,
                    body=experiment.model_dump(mode="json"),
                    created_at=now,
                )
            )
            self._insert_assertions(connection, experiment, created_version=1)
        return experiment, False

    def get(self, experiment_id: str) -> Experiment | None:
        with self._engine.connect() as connection:
            return self._load_version(connection, experiment_id)

    def append(
        self,
        experiment: Experiment,
        *,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[Experiment, bool]:
        now = self._clock()
        with self._engine.begin() as connection:
            replay = self._reserve_idempotency(
                connection,
                scope=_aggregate_scope(_ASSERTION_SCOPE, experiment.experiment_id),
                key=idempotency_key,
                request_hash=request_hash,
                response={
                    "experiment_id": experiment.experiment_id,
                    "experiment_version": experiment.version,
                },
            )
            if replay is not None:
                stored = self._load_version(
                    connection,
                    str(replay["experiment_id"]),
                    _response_int(replay["experiment_version"], "experiment_version"),
                )
                if stored is None:
                    raise RuntimeError("assertion idempotency record names a missing version")
                return stored, True
            self._lock_expected_version(connection, experiment.experiment_id, expected_version)
            if experiment.version != expected_version + 1:
                raise ValueError("appended experiment is not the next version")
            connection.execute(
                insert(experiment_versions).values(
                    experiment_id=experiment.experiment_id,
                    version=experiment.version,
                    supersedes_version=expected_version,
                    body=experiment.model_dump(mode="json"),
                    created_at=now,
                )
            )
            self._insert_assertions(connection, experiment, created_version=experiment.version)
            connection.execute(
                update(experiments)
                .where(experiments.c.experiment_id == experiment.experiment_id)
                .values(current_version=experiment.version, updated_at=now)
            )
        return experiment, False

    @staticmethod
    def _insert_validation(
        connection: Connection, validation: ValidationRun, now: datetime
    ) -> None:
        inserted_id = connection.execute(
            insert(validation_runs)
            .values(
                validation_id=validation.validation_id,
                experiment_id=validation.experiment_id,
                experiment_version=validation.experiment_version,
                schema_version=validation.schema_version,
                validation_version=validation.validation_version,
                release_status=validation.release_decision.status,
                body=validation.model_dump(mode="json"),
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=[validation_runs.c.validation_id])
            .returning(validation_runs.c.validation_id)
        ).scalar_one_or_none()
        if inserted_id is None:
            return
        for finding in validation.findings:
            connection.execute(
                insert(validation_findings).values(
                    finding_id=finding.finding_id,
                    validation_id=validation.validation_id,
                    experiment_id=validation.experiment_id,
                    field_name=finding.field_name,
                    severity=finding.severity,
                    requirement_class=finding.requirement_class,
                    body=finding.model_dump(mode="json"),
                )
            )

    def store_validation(
        self,
        validation: ValidationRun,
        *,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ValidationRun, bool]:
        now = self._clock()
        with self._engine.begin() as connection:
            replay = self._reserve_idempotency(
                connection,
                scope=_aggregate_scope(_VALIDATION_SCOPE, validation.experiment_id),
                key=idempotency_key,
                request_hash=request_hash,
                response={"validation_id": validation.validation_id},
            )
            if replay is not None:
                body = connection.execute(
                    select(validation_runs.c.body).where(
                        validation_runs.c.validation_id == str(replay["validation_id"])
                    )
                ).scalar_one_or_none()
                if body is not None:
                    return ValidationRun.model_validate(body), True
            self._lock_expected_version(connection, validation.experiment_id, expected_version)
            self._insert_validation(connection, validation, now)
        return validation, False

    def _reserve_object(
        self,
        connection: Connection,
        *,
        key: str,
        data: bytes,
        media_type: str,
        now: datetime,
    ) -> str:
        object_uri = f"s3://{self._store.bucket}/{key}"
        expected_sha = digest(data)
        connection.execute(
            insert(storage_objects)
            .values(
                object_uri=object_uri,
                bucket=self._store.bucket,
                object_key=key,
                byte_size=len(data),
                sha256=expected_sha,
                media_type=media_type,
                state="pending",
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=[storage_objects.c.object_uri])
        )
        row = connection.execute(
            select(
                storage_objects.c.byte_size,
                storage_objects.c.sha256,
                storage_objects.c.state,
            ).where(storage_objects.c.object_uri == object_uri)
        ).one()
        if row.byte_size not in {None, len(data)} or row.sha256 not in {None, expected_sha}:
            raise ValueError(
                "released object identity conflicts with retained metadata: "
                f"retained size={row.byte_size} sha256={row.sha256}; "
                f"requested size={len(data)} sha256={expected_sha}"
            )
        return object_uri

    @staticmethod
    def _commit_object(
        connection: Connection,
        *,
        object_uri: str,
        media_type: str,
        byte_size: int,
        sha256: str,
        now: datetime,
    ) -> None:
        connection.execute(
            update(storage_objects)
            .where(storage_objects.c.object_uri == object_uri)
            .values(
                byte_size=byte_size,
                sha256=sha256,
                media_type=media_type,
                state="committed",
                classification="accepted_evidence",
                classification_reason="released immutable Experiment Passport or Package",
                reconciled_at=now,
                committed_at=now,
            )
        )

    @staticmethod
    def _validation_from_passport(passport: ExperimentPassport) -> ValidationRun:
        return ValidationRun(
            validation_id=passport.validation_id,
            schema_version="1",
            validation_version=passport.validation_version,
            experiment_id=passport.experiment_id,
            experiment_version=passport.experiment_version,
            findings=passport.findings,
            release_decision=passport.release_decision,
        )

    def store_passport(
        self,
        passport: ExperimentPassport,
        *,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ExperimentPassport, bool]:
        json_key = f"experiment-passports/{passport.passport_id}/passport.json"
        html_key = f"experiment-passports/{passport.passport_id}/passport.html"
        now = self._clock()
        if passport.released_at is None:
            raise ValueError("a persisted Passport requires released_at")
        with self._engine.begin() as connection:
            replay = self._reserve_idempotency(
                connection,
                scope=_aggregate_scope(_PASSPORT_SCOPE, passport.experiment_id),
                key=idempotency_key,
                request_hash=request_hash,
                response={
                    "passport_id": passport.passport_id,
                    "released_at": passport.released_at.isoformat(),
                },
            )
            existing_body = connection.execute(
                select(experiment_passports.c.body).where(
                    experiment_passports.c.passport_id == passport.passport_id
                )
            ).scalar_one_or_none()
            if replay is not None and existing_body is not None:
                return ExperimentPassport.model_validate(existing_body), True
            effective_passport = passport
            if replay is not None:
                released_at = datetime.fromisoformat(str(replay["released_at"]))
                effective_passport = passport.model_copy(update={"released_at": released_at})
            self._lock_expected_version(connection, passport.experiment_id, expected_version)
            if existing_body is not None:
                return ExperimentPassport.model_validate(existing_body), True
            json_bytes = render_passport_json(effective_passport)
            html_bytes = render_passport_html(effective_passport)
            json_uri = self._reserve_object(
                connection,
                key=json_key,
                data=json_bytes,
                media_type="application/json",
                now=now,
            )
            html_uri = self._reserve_object(
                connection,
                key=html_key,
                data=html_bytes,
                media_type="text/html",
                now=now,
            )
            if replay is not None:
                body = connection.execute(
                    select(experiment_passports.c.body).where(
                        experiment_passports.c.passport_id == str(replay["passport_id"])
                    )
                ).scalar_one_or_none()
                if body is not None:
                    return ExperimentPassport.model_validate(body), True
        stored_json = self._store.put_and_verify(
            json_key, json_bytes, media_type="application/json"
        )
        stored_html = self._store.put_and_verify(html_key, html_bytes, media_type="text/html")
        with self._engine.begin() as connection:
            existing_body = connection.execute(
                select(experiment_passports.c.body).where(
                    experiment_passports.c.passport_id == passport.passport_id
                )
            ).scalar_one_or_none()
            if existing_body is not None:
                return ExperimentPassport.model_validate(existing_body), True
            self._lock_expected_version(connection, passport.experiment_id, expected_version)
            self._insert_validation(
                connection, self._validation_from_passport(effective_passport), now
            )
            self._commit_object(
                connection,
                object_uri=json_uri,
                media_type="application/json",
                byte_size=stored_json.byte_size,
                sha256=stored_json.sha256,
                now=now,
            )
            self._commit_object(
                connection,
                object_uri=html_uri,
                media_type="text/html",
                byte_size=stored_html.byte_size,
                sha256=stored_html.sha256,
                now=now,
            )
            inserted_id = connection.execute(
                insert(experiment_passports)
                .values(
                    passport_id=passport.passport_id,
                    experiment_id=passport.experiment_id,
                    experiment_version=passport.experiment_version,
                    validation_id=passport.validation_id,
                    schema_version=passport.schema_version,
                    supersedes_passport_id=passport.supersedes_passport_id,
                    body=effective_passport.model_dump(mode="json"),
                    json_object_uri=json_uri,
                    html_object_uri=html_uri,
                    json_sha256=stored_json.sha256,
                    html_sha256=stored_html.sha256,
                    released_at=effective_passport.released_at,
                )
                .on_conflict_do_nothing(index_elements=[experiment_passports.c.passport_id])
                .returning(experiment_passports.c.passport_id)
            ).scalar_one_or_none()
            if inserted_id is None:
                body = connection.execute(
                    select(experiment_passports.c.body).where(
                        experiment_passports.c.passport_id == passport.passport_id
                    )
                ).scalar_one()
                return ExperimentPassport.model_validate(body), True
        return effective_passport, False

    def latest_passport(self, experiment_id: str) -> ExperimentPassport | None:
        with self._engine.connect() as connection:
            body = connection.execute(
                select(experiment_passports.c.body)
                .where(experiment_passports.c.experiment_id == experiment_id)
                .order_by(experiment_passports.c.experiment_version.desc())
                .limit(1)
            ).scalar_one_or_none()
        return ExperimentPassport.model_validate(body) if body is not None else None

    def get_passport(self, passport_id: str) -> ExperimentPassport | None:
        with self._engine.connect() as connection:
            body = connection.execute(
                select(experiment_passports.c.body).where(
                    experiment_passports.c.passport_id == passport_id
                )
            ).scalar_one_or_none()
        return ExperimentPassport.model_validate(body) if body is not None else None

    def store_package(
        self,
        package: ExperimentPackage,
        archive_bytes: bytes,
        *,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ExperimentPackage, bool]:
        if digest(archive_bytes) != package.archive_sha256:
            raise ValueError("Experiment Package bytes do not match their declared checksum")
        key = f"experiment-packages/{package.package_id}.zip"
        now = self._clock()
        with self._engine.begin() as connection:
            replay = self._reserve_idempotency(
                connection,
                scope=_aggregate_scope(_PACKAGE_SCOPE, package.experiment_id),
                key=idempotency_key,
                request_hash=request_hash,
                response={"package_id": package.package_id},
            )
            object_uri = self._reserve_object(
                connection,
                key=key,
                data=archive_bytes,
                media_type="application/zip",
                now=now,
            )
            if replay is not None:
                body = connection.execute(
                    select(experiment_packages.c.body).where(
                        experiment_packages.c.package_id == str(replay["package_id"])
                    )
                ).scalar_one_or_none()
                if body is not None:
                    return ExperimentPackage.model_validate(body), True
            self._lock_expected_version(connection, package.experiment_id, expected_version)
        stored = self._store.put_and_verify(key, archive_bytes, media_type="application/zip")
        with self._engine.begin() as connection:
            existing_body = connection.execute(
                select(experiment_packages.c.body).where(
                    experiment_packages.c.package_id == package.package_id
                )
            ).scalar_one_or_none()
            if existing_body is not None:
                return ExperimentPackage.model_validate(existing_body), True
            self._lock_expected_version(connection, package.experiment_id, expected_version)
            self._commit_object(
                connection,
                object_uri=object_uri,
                media_type="application/zip",
                byte_size=stored.byte_size,
                sha256=stored.sha256,
                now=now,
            )
            inserted_id = connection.execute(
                insert(experiment_packages)
                .values(
                    package_id=package.package_id,
                    passport_id=package.passport_id,
                    experiment_id=package.experiment_id,
                    experiment_version=package.experiment_version,
                    schema_version=package.schema_version,
                    supersedes_package_id=package.supersedes_package_id,
                    object_uri=object_uri,
                    archive_sha256=package.archive_sha256,
                    archive_byte_size=package.archive_byte_size,
                    body=package.model_dump(mode="json"),
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=[experiment_packages.c.package_id])
                .returning(experiment_packages.c.package_id)
            ).scalar_one_or_none()
            if inserted_id is None:
                body = connection.execute(
                    select(experiment_packages.c.body).where(
                        experiment_packages.c.package_id == package.package_id
                    )
                ).scalar_one()
                return ExperimentPackage.model_validate(body), True
        return package, False

    def latest_package(self, experiment_id: str) -> ExperimentPackage | None:
        with self._engine.connect() as connection:
            body = connection.execute(
                select(experiment_packages.c.body)
                .where(experiment_packages.c.experiment_id == experiment_id)
                .order_by(experiment_packages.c.experiment_version.desc())
                .limit(1)
            ).scalar_one_or_none()
        return ExperimentPackage.model_validate(body) if body is not None else None

    def get_package(self, package_id: str) -> tuple[ExperimentPackage, bytes] | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(experiment_packages.c.body, experiment_packages.c.object_uri).where(
                    experiment_packages.c.package_id == package_id
                )
            ).one_or_none()
        if row is None:
            return None
        package = ExperimentPackage.model_validate(row.body)
        prefix = f"s3://{self._store.bucket}/"
        if not row.object_uri.startswith(prefix):
            raise ValueError("Experiment Package object URI names another bucket")
        archive_bytes = self._store.get(row.object_uri.removeprefix(prefix))
        return package, archive_bytes


__all__ = ["PostgresExperimentRepository"]

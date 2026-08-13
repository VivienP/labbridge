"""Generic CV persistence across real PostgreSQL and S3-compatible object storage."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Never

import pytest
from sqlalchemy import Engine, delete, func, select, update

from labbridge.application.cv_ingestion import (
    CVIngestionService,
    NormalisedObservationIntegrityError,
    ParserRecordIntegrityError,
)
from labbridge.application.source_intake import IntakeSource, SourceArtifactService
from labbridge.domain.cv import ColumnMapping, CVImportProfile, CVMetadata
from labbridge.infrastructure.gamry_dta import GamryDtaParseError
from labbridge.infrastructure.objectstore import S3ObjectStore
from labbridge.infrastructure.persistence.cv import PostgresCVRecordRepository
from labbridge.infrastructure.persistence.source_artifacts import PostgresSourceArtifactRepository
from labbridge.infrastructure.persistence.tables import (
    cv_parser_records,
    cv_structural_findings,
    cv_transformation_records,
    idempotency_keys,
    import_profiles,
    normalised_cv_observations,
    source_artifacts,
    storage_objects,
)
from labbridge.runtime.reconciliation import classify_objects

pytestmark = pytest.mark.integration


class SimulatedProcessStopError(RuntimeError):
    pass


class StopAfterUploadStore:
    def __init__(self, inner: S3ObjectStore, engine: Engine) -> None:
        self._inner = inner
        self._engine = engine
        self.bucket = inner.bucket
        self.staged_before_upload = False
        self.uploaded_uri: str | None = None

    def put_and_verify(self, key: str, data: bytes, *, media_type: str) -> Never:
        object_uri = f"s3://{self.bucket}/{key}"
        with self._engine.connect() as connection:
            row = connection.execute(
                select(
                    storage_objects.c.state,
                    storage_objects.c.byte_size,
                    storage_objects.c.sha256,
                ).where(storage_objects.c.object_uri == object_uri)
            ).one()
        assert row.state == "pending"
        assert row.byte_size == len(data)
        assert row.sha256 == hashlib.sha256(data).hexdigest()
        self.staged_before_upload = True
        stored = self._inner.put_and_verify(key, data, media_type=media_type)
        self.uploaded_uri = stored.uri
        raise SimulatedProcessStopError

    def get(self, key: str) -> bytes:
        return self._inner.get(key)

    def exists(self, key: str) -> bool:
        return self._inner.exists(key)


def _profile() -> CVImportProfile:
    return CVImportProfile(
        schema_version="1",
        technique="cyclic_voltammetry",
        environment_id="synthetic_cv_fixture",
        encoding="utf-8",
        delimiter=",",
        decimal_convention="point",
        header_row=1,
        missing_value_tokens=("", "NA"),
        columns=(
            ColumnMapping(source_column="E", role="potential", source_unit="mV", target_unit="V"),
            ColumnMapping(source_column="I", role="current", source_unit="mA", target_unit="A"),
            ColumnMapping(source_column="note", role="ignored"),
        ),
        metadata=CVMetadata.unknown(),
    )


def _dta_profile() -> CVImportProfile:
    return CVImportProfile(
        schema_version="1",
        technique="cyclic_voltammetry",
        environment_id="synthetic_gamry_cv_fixture",
        encoding="utf-8",
        delimiter="\t",
        decimal_convention="point",
        header_row=7,
        missing_value_tokens=("",),
        columns=(
            ColumnMapping(source_column="Pt", role="ignored"),
            ColumnMapping(source_column="T", role="time", source_unit="s", target_unit="s"),
            ColumnMapping(
                source_column="Vf",
                role="potential",
                source_unit="V vs. Ref.",
                target_unit="V",
            ),
            ColumnMapping(source_column="Im", role="current", source_unit="A", target_unit="A"),
            ColumnMapping(source_column="Vu", role="ignored"),
            ColumnMapping(source_column="Sig", role="ignored"),
            ColumnMapping(source_column="Ach", role="ignored"),
            ColumnMapping(source_column="IERange", role="ignored"),
            ColumnMapping(source_column="Over", role="ignored"),
            ColumnMapping(source_column="Cycle", role="cycle", source_unit="#", target_unit="1"),
            ColumnMapping(source_column="Temp", role="ignored"),
        ),
        metadata=CVMetadata.unknown(),
    )


def _dta_payload(version: str) -> bytes:
    return (
        "EXPLAIN\r\n"
        "TAG\tCV\r\n"
        "TITLE\tLABEL\tSynthetic CV\tTest Identifier\r\n"
        f"FRAMEWORKVERSION\tQUANT\t{version}\tFramework Version\r\n"
        "NOTES\tNOTES\t0\tSynthetic fixture\r\n"
        "CURVE\tTABLE\t2\r\n"
        "\tPt\tT\tVf\tIm\tVu\tSig\tAch\tIERange\tOver\tCycle\tTemp\r\n"
        "\t#\ts\tV vs. Ref.\tA\tV\tV\tV\t#\tbits\t#\tdeg C\r\n"
        "\t0\t0.00\t-0.240\t0.012\t0\t-0.24\t0\t9\t..........\t0\t25.0\r\n"
        "\t1\t0.10\t0.120\t-0.031\t0\t0.12\t0\t9\t..........\t1\t25.0\r\n"
    ).encode()


def test_accepted_and_rejected_dta_parser_records_are_durable(
    migrated: Engine, object_store: S3ObjectStore
) -> None:
    marker = uuid.uuid4().hex
    now = datetime(2026, 8, 12, tzinfo=UTC)
    source_service = SourceArtifactService(
        PostgresSourceArtifactRepository(migrated), object_store, clock=lambda: now
    )
    accepted_source = source_service.intake(
        IntakeSource(
            intake_id=f"phase4-accepted:{marker}",
            data=_dta_payload("7.07"),
            filename=f"synthetic-gamry-{marker}.dta",
            media_type="application/vnd.gamry.dta",
            data_origin="synthetic",
            execution_mode="replay",
        )
    )
    rejected_source = source_service.intake(
        IntakeSource(
            intake_id=f"phase4-rejected:{marker}",
            data=_dta_payload("7.08"),
            filename=f"unsupported-gamry-{marker}.dta",
            media_type="application/vnd.gamry.dta",
            data_origin="synthetic",
            execution_mode="replay",
        )
    )
    records = PostgresCVRecordRepository(migrated, object_store, clock=lambda: now)
    service = CVIngestionService(source_service, records, producing_version="0.1.0")
    stored_profile = service.create_profile(_dta_profile())

    with pytest.raises(GamryDtaParseError) as caught:
        service.normalise(
            rejected_source.artifact.source_artifact_id,
            stored_profile.profile_id,
            source_format="gamry_dta",
        )
    rejected_record_id = caught.value.parser_record_id
    assert service.get_parser_record(rejected_record_id).record == caught.value.record

    accepted = service.normalise(
        accepted_source.artifact.source_artifact_id,
        stored_profile.profile_id,
        source_format="gamry_dta",
    )
    accepted_record = accepted.result.parser_record
    assert accepted_record is not None
    assert records.get_normalisation(accepted.result.observation.observation_id) == accepted.result
    with migrated.connect() as connection:
        rows = connection.execute(
            select(
                cv_parser_records.c.parser_record_id,
                cv_parser_records.c.status,
                cv_parser_records.c.observation_id,
            ).where(
                cv_parser_records.c.parser_record_id.in_(
                    (rejected_record_id, accepted_record.parser_record_id)
                )
            )
        ).all()
    assert sorted((row.status, row.observation_id) for row in rows) == [
        ("accepted", accepted.result.observation.observation_id),
        ("rejected", None),
    ]

    tampered_body = caught.value.record.model_dump(mode="json")
    tampered_body["support_statement"] = "tampered parser evidence"
    with migrated.begin() as connection:
        connection.execute(
            update(cv_parser_records)
            .where(cv_parser_records.c.parser_record_id == rejected_record_id)
            .values(body=tampered_body)
        )
    with pytest.raises(ParserRecordIntegrityError):
        service.get_parser_record(rejected_record_id)
    with migrated.connect() as connection:
        retained_tampered_body = connection.execute(
            select(cv_parser_records.c.body).where(
                cv_parser_records.c.parser_record_id == rejected_record_id
            )
        ).scalar_one()
    assert retained_tampered_body == tampered_body

    with migrated.begin() as connection:
        observation_id = accepted.result.observation.observation_id
        object_uri = connection.execute(
            select(normalised_cv_observations.c.object_uri).where(
                normalised_cv_observations.c.observation_id == observation_id
            )
        ).scalar_one()
        connection.execute(
            delete(cv_parser_records).where(
                cv_parser_records.c.parser_record_id.in_(
                    (rejected_record_id, accepted_record.parser_record_id)
                )
            )
        )
        connection.execute(
            delete(cv_transformation_records).where(
                cv_transformation_records.c.observation_id == observation_id
            )
        )
        connection.execute(
            delete(cv_structural_findings).where(
                cv_structural_findings.c.observation_id == observation_id
            )
        )
        connection.execute(
            delete(normalised_cv_observations).where(
                normalised_cv_observations.c.observation_id == observation_id
            )
        )
        connection.execute(
            delete(import_profiles).where(import_profiles.c.profile_id == stored_profile.profile_id)
        )
        connection.execute(
            delete(idempotency_keys).where(
                idempotency_keys.c.idempotency_key.in_(
                    (
                        f"source.intake:phase4-accepted:{marker}",
                        f"source.intake:phase4-rejected:{marker}",
                    )
                )
            )
        )
        source_ids = (
            accepted_source.artifact.source_artifact_id,
            rejected_source.artifact.source_artifact_id,
        )
        connection.execute(
            delete(source_artifacts).where(source_artifacts.c.source_artifact_id.in_(source_ids))
        )
        connection.execute(
            delete(storage_objects).where(
                storage_objects.c.object_uri.in_(
                    (
                        object_uri,
                        accepted_source.artifact.object_uri,
                        rejected_source.artifact.object_uri,
                    )
                )
            )
        )


def test_profile_observation_transformations_and_findings_are_retained(
    migrated: Engine, object_store: S3ObjectStore
) -> None:
    marker = uuid.uuid4().hex
    payload = f"E,I,note\n-100,2,{marker}\n100,-2,{marker}\n".encode()
    now = datetime(2026, 8, 12, tzinfo=UTC)
    source_service = SourceArtifactService(
        PostgresSourceArtifactRepository(migrated), object_store, clock=lambda: now
    )
    source_result = source_service.intake(
        IntakeSource(
            intake_id=f"phase2:{marker}",
            data=payload,
            filename=f"synthetic-replay-cv-{marker}.csv",
            media_type="text/csv",
            data_origin="synthetic",
            execution_mode="replay",
        )
    )
    records = PostgresCVRecordRepository(migrated, object_store, clock=lambda: now)
    service = CVIngestionService(source_service, records, producing_version="0.1.0")

    stored_profile = service.create_profile(_profile())
    first = service.normalise(source_result.artifact.source_artifact_id, stored_profile.profile_id)
    second = service.normalise(source_result.artifact.source_artifact_id, stored_profile.profile_id)
    observation_id = first.result.observation.observation_id

    assert not first.replayed
    assert second.replayed
    assert records.get_normalisation(observation_id) == first.result
    with migrated.connect() as connection:
        assert connection.execute(
            select(func.count())
            .select_from(cv_transformation_records)
            .where(cv_transformation_records.c.observation_id == observation_id)
        ).scalar_one() == len(first.result.graph.records)
        assert connection.execute(
            select(func.count())
            .select_from(cv_structural_findings)
            .where(cv_structural_findings.c.observation_id == observation_id)
        ).scalar_one() == len(first.result.findings)

    with migrated.connect() as connection:
        normalised_uri = connection.execute(
            select(normalised_cv_observations.c.object_uri).where(
                normalised_cv_observations.c.observation_id == observation_id
            )
        ).scalar_one()
    object_key = normalised_uri.removeprefix(f"s3://{object_store.bucket}/")
    original_payload = object_store.get(object_key)
    object_store._client.put_object(
        Bucket=object_store.bucket,
        Key=object_key,
        Body=b"tampered normalised observation",
        ContentType="application/json",
    )
    with pytest.raises(NormalisedObservationIntegrityError):
        records.get_normalisation(observation_id)
    object_store._client.put_object(
        Bucket=object_store.bucket,
        Key=object_key,
        Body=original_payload,
        ContentType="application/json",
    )

    with migrated.begin() as connection:
        object_uris = (
            connection.execute(
                select(normalised_cv_observations.c.object_uri).where(
                    normalised_cv_observations.c.observation_id == observation_id
                )
            )
            .scalars()
            .all()
        )
        connection.execute(
            delete(cv_transformation_records).where(
                cv_transformation_records.c.observation_id == observation_id
            )
        )
        connection.execute(
            delete(cv_structural_findings).where(
                cv_structural_findings.c.observation_id == observation_id
            )
        )
        connection.execute(
            delete(normalised_cv_observations).where(
                normalised_cv_observations.c.observation_id == observation_id
            )
        )
        connection.execute(
            delete(import_profiles).where(import_profiles.c.profile_id == stored_profile.profile_id)
        )
        connection.execute(
            delete(idempotency_keys).where(
                idempotency_keys.c.idempotency_key == f"source.intake:phase2:{marker}"
            )
        )
        connection.execute(
            delete(source_artifacts).where(
                source_artifacts.c.source_artifact_id == source_result.artifact.source_artifact_id
            )
        )
        connection.execute(
            delete(storage_objects).where(storage_objects.c.object_uri.in_(object_uris))
        )
        connection.execute(
            delete(storage_objects).where(
                storage_objects.c.object_uri == source_result.artifact.object_uri
            )
        )


def test_upload_interruption_leaves_a_reconcilable_pending_object(
    migrated: Engine, object_store: S3ObjectStore
) -> None:
    marker = uuid.uuid4().hex
    payload = f"E,I,note\n-100,2,{marker}\n100,-2,{marker}\n".encode()
    now = datetime(2026, 8, 12, tzinfo=UTC)
    source_service = SourceArtifactService(
        PostgresSourceArtifactRepository(migrated), object_store, clock=lambda: now
    )
    source_result = source_service.intake(
        IntakeSource(
            intake_id=f"phase2-interrupted:{marker}",
            data=payload,
            filename=f"synthetic-replay-cv-interrupted-{marker}.csv",
            media_type="text/csv",
            data_origin="synthetic",
            execution_mode="replay",
        )
    )
    stopping_store = StopAfterUploadStore(object_store, migrated)
    records = PostgresCVRecordRepository(migrated, stopping_store, clock=lambda: now)
    service = CVIngestionService(source_service, records, producing_version="0.1.0")
    stored_profile = service.create_profile(_profile())

    with pytest.raises(SimulatedProcessStopError):
        service.normalise(source_result.artifact.source_artifact_id, stored_profile.profile_id)

    assert stopping_store.staged_before_upload
    assert stopping_store.uploaded_uri is not None
    with migrated.begin() as connection:
        classified, unreachable = classify_objects(connection, object_store)
    matching = [item for item in classified if item.object_uri == stopping_store.uploaded_uri]
    assert not unreachable
    assert [(item.classification) for item in matching] == ["diagnostic_orphan"]
    with migrated.connect() as connection:
        state, classification = connection.execute(
            select(storage_objects.c.state, storage_objects.c.classification).where(
                storage_objects.c.object_uri == stopping_store.uploaded_uri
            )
        ).one()
    assert state == "pending"
    assert classification == "diagnostic_orphan"

    with migrated.begin() as connection:
        connection.execute(
            delete(import_profiles).where(import_profiles.c.profile_id == stored_profile.profile_id)
        )
        connection.execute(
            delete(idempotency_keys).where(
                idempotency_keys.c.idempotency_key == f"source.intake:phase2-interrupted:{marker}"
            )
        )
        connection.execute(
            delete(source_artifacts).where(
                source_artifacts.c.source_artifact_id == source_result.artifact.source_artifact_id
            )
        )
        connection.execute(
            delete(storage_objects).where(
                storage_objects.c.object_uri.in_(
                    [stopping_store.uploaded_uri, source_result.artifact.object_uri]
                )
            )
        )

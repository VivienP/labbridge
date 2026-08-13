"""Galvanostatic electrolysis persistence over real PostgreSQL and MinIO."""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError

from electrolysis_helpers import (
    auxiliary_source,
    electrolysis_profile,
    electrolysis_profile_with_auxiliary,
)
from labbridge.application.electrolysis_ingestion import (
    ElectrolysisIngestionService,
    normalise_electrolysis,
)
from labbridge.application.experiments import ExperimentService
from labbridge.application.source_intake import (
    IntakeSource,
    SourceArtifactService,
    SourceIntegrityError,
    SourceNotFoundError,
)
from labbridge.evidence.experiment_package import verify_experiment_package
from labbridge.evidence.manifest import canonical_json, digest
from labbridge.infrastructure.objectstore import S3ObjectStore
from labbridge.infrastructure.persistence.electrolysis import (
    PostgresElectrolysisRecordRepository,
)
from labbridge.infrastructure.persistence.experiments import PostgresExperimentRepository
from labbridge.infrastructure.persistence.source_artifacts import PostgresSourceArtifactRepository
from labbridge.infrastructure.persistence.tables import (
    electrolysis_auxiliary_results,
    electrolysis_structural_findings,
    electrolysis_transformation_records,
    experiment_packages,
    idempotency_keys,
    normalised_electrolysis_observations,
    storage_objects,
)
from labbridge.runtime.reconciliation import classify_objects

pytestmark = pytest.mark.integration


def test_profile_observation_transformations_and_findings_are_durable(
    migrated: Engine, object_store: S3ObjectStore
) -> None:
    marker = uuid.uuid4().hex
    first_potential = f"-0.{int(marker[:8], 16):010d}"
    payload = (
        "elapsed,applied_current,working_potential\n"
        f"0,10.0,{first_potential}\n60,10.0,-0.435\n120,10.0,-0.447\n"
    ).encode()
    now = datetime(2026, 8, 13, tzinfo=UTC)
    source_service = SourceArtifactService(
        PostgresSourceArtifactRepository(migrated), object_store, clock=lambda: now
    )
    source = source_service.intake(
        IntakeSource(
            intake_id=f"phase5:{marker}",
            data=payload,
            filename=f"synthetic-electrolysis-{marker}.csv",
            media_type="text/csv",
            data_origin="synthetic",
            execution_mode="replay",
        )
    )
    records = PostgresElectrolysisRecordRepository(migrated, object_store, clock=lambda: now)
    service = ElectrolysisIngestionService(source_service, records, producing_version="0.1.0")
    stored_profile = service.create_profile(electrolysis_profile())

    first = service.normalise(source.artifact.source_artifact_id, stored_profile.profile_id)
    second = service.normalise(source.artifact.source_artifact_id, stored_profile.profile_id)
    observation_id = first.result.observation.observation_id

    assert first.replayed is False
    assert second.replayed is True
    retained = records.get_normalisation(observation_id)
    assert retained is not None
    assert retained == first.result
    with migrated.connect() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(normalised_electrolysis_observations)
                .where(normalised_electrolysis_observations.c.observation_id == observation_id)
            ).scalar_one()
            == 1
        )
        assert connection.execute(
            select(func.count())
            .select_from(electrolysis_transformation_records)
            .where(electrolysis_transformation_records.c.observation_id == observation_id)
        ).scalar_one() == len(first.result.graph.records)
        assert connection.execute(
            select(func.count())
            .select_from(electrolysis_structural_findings)
            .where(electrolysis_structural_findings.c.observation_id == observation_id)
        ).scalar_one() == len(first.result.findings)
    with migrated.begin() as connection:
        classified, unreachable = classify_objects(connection, object_store)
    retained_uri = next(
        item.object_uri
        for item in classified
        if item.object_uri.startswith(f"s3://{object_store.bucket}/normalised-electrolysis/sha256/")
    )
    retained_verdict = next(item for item in classified if item.object_uri == retained_uri)
    assert not unreachable
    assert retained_verdict.classification == "accepted_evidence"


def test_postgres_minio_passport_and_package_complete_the_electrolysis_slice(
    migrated: Engine, object_store: S3ObjectStore
) -> None:
    marker = uuid.uuid4().hex
    payload = (
        "elapsed,applied_current,working_potential\n"
        f"0,10.0,-0.{int(marker[:8], 16):010d}\n"
        "60,10.0,-0.435\n120,10.0,-0.447\n"
    ).encode()
    now = datetime(2026, 8, 13, 14, 0, tzinfo=UTC)
    source_service = SourceArtifactService(
        PostgresSourceArtifactRepository(migrated), object_store, clock=lambda: now
    )
    source = source_service.intake(
        IntakeSource(
            intake_id=f"phase5-package:{marker}",
            data=payload,
            filename=f"synthetic-electrolysis-package-{marker}.csv",
            media_type="text/csv",
            data_origin="synthetic",
            execution_mode="replay",
        )
    )
    auxiliary_fixture = auxiliary_source(
        (f"sample,analyte,concentration,unit\nS-{marker},product_a,0.52,mol/L\n").encode()
    )
    auxiliary = source_service.intake(
        IntakeSource(
            intake_id=f"phase5-auxiliary:{marker}",
            data=auxiliary_fixture.data,
            filename=auxiliary_fixture.artifact.filename,
            media_type=auxiliary_fixture.artifact.media_type,
            data_origin="synthetic",
            execution_mode="replay",
        )
    )
    electrolysis_service = ElectrolysisIngestionService(
        source_service,
        PostgresElectrolysisRecordRepository(migrated, object_store, clock=lambda: now),
        producing_version="0.1.0",
    )
    profile = electrolysis_service.create_profile(
        electrolysis_profile_with_auxiliary(
            electrical_source_artifact_id=source.artifact.source_artifact_id,
            auxiliary=auxiliary_fixture,
        )
    )
    normalisation = electrolysis_service.normalise(
        source.artifact.source_artifact_id, profile.profile_id
    )
    experiment_service = ExperimentService(
        electrolysis_service,
        source_service,
        PostgresExperimentRepository(migrated, object_store, clock=lambda: now),
        clock=lambda: now,
        producing_versions={"labbridge": "0.1.0", "experiment_package": "3"},
    )

    experiment = experiment_service.create_experiment(
        normalisation.result.observation.observation_id,
        expected_version=0,
        idempotency_key=f"phase5-experiment:{marker}",
    ).experiment
    validation = experiment_service.run_validation(
        experiment.experiment_id,
        expected_version=1,
        idempotency_key=f"phase5-validation:{marker}",
    ).validation
    passport = experiment_service.release_passport(
        experiment.experiment_id,
        expected_version=1,
        idempotency_key=f"phase5-passport:{marker}",
    ).passport
    package = experiment_service.create_package(
        experiment.experiment_id,
        passport_id=passport.passport_id,
        expected_version=1,
        idempotency_key=f"phase5-package:{marker}",
    ).package
    archive = experiment_service.download_package(package.package_id)

    assert validation.release_decision.status == "eligible"
    assert package.schema_version == "3"
    assert verify_experiment_package(archive).lineage_closed is True
    with zipfile.ZipFile(io.BytesIO(archive), "r") as package_archive:
        assert any(name.startswith("auxiliary-source/") for name in package_archive.namelist())
        assert auxiliary_fixture.data in {
            package_archive.read(name)
            for name in package_archive.namelist()
            if name.startswith("auxiliary-source/")
        }
    with migrated.connect() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(electrolysis_auxiliary_results)
                .where(
                    electrolysis_auxiliary_results.c.source_artifact_id
                    == auxiliary.artifact.source_artifact_id
                )
            ).scalar_one()
            == 1
        )


def test_missing_auxiliary_source_leaves_no_normalisation_or_package(
    migrated: Engine, object_store: S3ObjectStore
) -> None:
    marker = uuid.uuid4().hex
    now = datetime(2026, 8, 13, 15, 0, tzinfo=UTC)
    source_service = SourceArtifactService(
        PostgresSourceArtifactRepository(migrated), object_store, clock=lambda: now
    )
    primary_payload = (
        b"elapsed,applied_current,working_potential\n"
        b"0,10.0,-0.420\n60,10.0,-0.435\n120,10.0,-0.447\n"
    )
    primary = source_service.intake(
        IntakeSource(
            intake_id=f"phase5-missing-auxiliary:{marker}",
            data=primary_payload,
            filename=f"electrolysis-missing-auxiliary-{marker}.csv",
            media_type="text/csv",
            data_origin="synthetic",
            execution_mode="replay",
        )
    )
    service = ElectrolysisIngestionService(
        source_service,
        PostgresElectrolysisRecordRepository(migrated, object_store, clock=lambda: now),
        producing_version="0.1.0",
    )
    missing_auxiliary = auxiliary_source(
        (f"sample,analyte,concentration,unit\nS-{marker},product_a,0.52,mol/L\n").encode()
    )
    profile = service.create_profile(
        electrolysis_profile_with_auxiliary(
            electrical_source_artifact_id=primary.artifact.source_artifact_id,
            auxiliary=missing_auxiliary,
        )
    )
    with migrated.connect() as connection:
        package_count_before = connection.execute(
            select(func.count()).select_from(experiment_packages)
        ).scalar_one()
        observation_count_before = connection.execute(
            select(func.count())
            .select_from(normalised_electrolysis_observations)
            .where(
                normalised_electrolysis_observations.c.source_artifact_id
                == primary.artifact.source_artifact_id
            )
        ).scalar_one()

    with pytest.raises(SourceNotFoundError):
        service.normalise(
            primary.artifact.source_artifact_id,
            profile.profile_id,
            idempotency_key=f"phase5-missing-auxiliary:{marker}",
        )

    assert source_service.retrieve(primary.artifact.source_artifact_id).data == primary_payload
    with migrated.connect() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(normalised_electrolysis_observations)
                .where(
                    normalised_electrolysis_observations.c.source_artifact_id
                    == primary.artifact.source_artifact_id
                )
            ).scalar_one()
            == observation_count_before
        )
        assert (
            connection.execute(select(func.count()).select_from(experiment_packages)).scalar_one()
            == package_count_before
        )
        assert (
            connection.execute(
                select(func.count())
                .select_from(idempotency_keys)
                .where(
                    idempotency_keys.c.scope == "electrolysis.normalise",
                    idempotency_keys.c.idempotency_key == f"phase5-missing-auxiliary:{marker}",
                )
            ).scalar_one()
            == 0
        )


def test_tampered_auxiliary_source_is_quarantined_before_normalisation(
    migrated: Engine, object_store: S3ObjectStore
) -> None:
    marker = uuid.uuid4().hex
    now = datetime(2026, 8, 13, 16, 0, tzinfo=UTC)
    source_service = SourceArtifactService(
        PostgresSourceArtifactRepository(migrated), object_store, clock=lambda: now
    )
    primary_fixture = (
        b"elapsed,applied_current,working_potential\n"
        b"0,10.0,-0.420\n60,10.0,-0.435\n120,10.0,-0.447\n"
    )
    primary = source_service.intake(
        IntakeSource(
            intake_id=f"phase5-tamper-primary:{marker}",
            data=primary_fixture,
            filename=f"electrolysis-tamper-{marker}.csv",
            media_type="text/csv",
            data_origin="synthetic",
            execution_mode="replay",
        )
    )
    auxiliary_fixture = auxiliary_source(
        (f"sample,analyte,concentration,unit\nS-{marker},product_a,0.52,mol/L\n").encode()
    )
    auxiliary = source_service.intake(
        IntakeSource(
            intake_id=f"phase5-tamper-auxiliary:{marker}",
            data=auxiliary_fixture.data,
            filename=auxiliary_fixture.artifact.filename,
            media_type=auxiliary_fixture.artifact.media_type,
            data_origin="synthetic",
            execution_mode="replay",
        )
    )
    auxiliary_key = auxiliary.artifact.object_uri.removeprefix(f"s3://{object_store.bucket}/")
    object_store._client.put_object(
        Bucket=object_store.bucket,
        Key=auxiliary_key,
        Body=b"tampered analytical bytes",
        ContentType="text/csv",
    )
    service = ElectrolysisIngestionService(
        source_service,
        PostgresElectrolysisRecordRepository(migrated, object_store, clock=lambda: now),
        producing_version="0.1.0",
    )
    profile = service.create_profile(
        electrolysis_profile_with_auxiliary(
            electrical_source_artifact_id=primary.artifact.source_artifact_id,
            auxiliary=auxiliary_fixture,
        )
    )
    with migrated.connect() as connection:
        observation_count_before = connection.execute(
            select(func.count())
            .select_from(normalised_electrolysis_observations)
            .where(
                normalised_electrolysis_observations.c.source_artifact_id
                == primary.artifact.source_artifact_id
            )
        ).scalar_one()
    with pytest.raises(SourceIntegrityError):
        service.normalise(primary.artifact.source_artifact_id, profile.profile_id)

    assert source_service.lookup(auxiliary.artifact.source_artifact_id).state == "quarantined"
    assert source_service.retrieve(primary.artifact.source_artifact_id).data == primary_fixture
    assert object_store.get(auxiliary_key) == b"tampered analytical bytes"
    with migrated.connect() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(normalised_electrolysis_observations)
                .where(
                    normalised_electrolysis_observations.c.source_artifact_id
                    == primary.artifact.source_artifact_id
                )
            ).scalar_one()
            == observation_count_before
        )


def test_auxiliary_insert_failure_rolls_back_and_retry_accepts_once(
    migrated: Engine, object_store: S3ObjectStore
) -> None:
    marker = uuid.uuid4().hex
    now = datetime(2026, 8, 13, 17, 0, tzinfo=UTC)
    source_service = SourceArtifactService(
        PostgresSourceArtifactRepository(migrated), object_store, clock=lambda: now
    )
    primary = source_service.intake(
        IntakeSource(
            intake_id=f"phase5-rollback-primary:{marker}",
            data=(
                b"elapsed,applied_current,working_potential\n"
                b"0,10.0,-0.420\n60,10.0,-0.435\n120,10.0,-0.447\n"
            ),
            filename=f"electrolysis-rollback-{marker}.csv",
            media_type="text/csv",
            data_origin="synthetic",
            execution_mode="replay",
        )
    )
    auxiliary_fixture = auxiliary_source(
        (f"sample,analyte,concentration,unit\nS-{marker},product_a,0.52,mol/L\n").encode()
    )
    source_service.intake(
        IntakeSource(
            intake_id=f"phase5-rollback-auxiliary:{marker}",
            data=auxiliary_fixture.data,
            filename=auxiliary_fixture.artifact.filename,
            media_type=auxiliary_fixture.artifact.media_type,
            data_origin="synthetic",
            execution_mode="replay",
        )
    )
    records = PostgresElectrolysisRecordRepository(migrated, object_store, clock=lambda: now)
    service = ElectrolysisIngestionService(source_service, records, producing_version="0.1.0")
    profile = service.create_profile(
        electrolysis_profile_with_auxiliary(
            electrical_source_artifact_id=primary.artifact.source_artifact_id,
            auxiliary=auxiliary_fixture,
        )
    )
    expected_result = normalise_electrolysis(
        source_service.retrieve(primary.artifact.source_artifact_id),
        profile.profile,
        producing_version="0.1.0",
        auxiliary_sources={
            auxiliary_fixture.artifact.source_artifact_id: source_service.retrieve(
                auxiliary_fixture.artifact.source_artifact_id
            )
        },
    )
    expected_payload = canonical_json(expected_result.model_dump(mode="json"))
    expected_uri = (
        f"s3://{object_store.bucket}/normalised-electrolysis/sha256/{digest(expected_payload)}.json"
    )
    key = f"phase5-rollback:{marker}"
    function_name = f"fail_electrolysis_auxiliary_{marker}"
    trigger_name = f"fail_electrolysis_auxiliary_{marker}"
    with migrated.begin() as connection:
        connection.execute(
            text(
                f"CREATE FUNCTION {function_name}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'injected auxiliary insert failure'; END $$"
            )
        )
        connection.execute(
            text(
                f"CREATE TRIGGER {trigger_name} BEFORE INSERT ON electrolysis_auxiliary_results "
                f"FOR EACH ROW EXECUTE FUNCTION {function_name}()"
            )
        )
    try:
        with pytest.raises(DBAPIError, match="injected auxiliary insert failure"):
            service.normalise(
                primary.artifact.source_artifact_id,
                profile.profile_id,
                idempotency_key=key,
            )
        with migrated.connect() as connection:
            pending_uri = connection.execute(
                select(storage_objects.c.object_uri).where(
                    storage_objects.c.object_uri == expected_uri,
                    storage_objects.c.state == "pending",
                )
            ).scalar_one()
            assert (
                connection.execute(
                    select(func.count())
                    .select_from(normalised_electrolysis_observations)
                    .where(
                        normalised_electrolysis_observations.c.observation_id
                        == expected_result.observation.observation_id
                    )
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    select(func.count())
                    .select_from(electrolysis_auxiliary_results)
                    .where(
                        electrolysis_auxiliary_results.c.observation_id
                        == expected_result.observation.observation_id
                    )
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    select(func.count())
                    .select_from(idempotency_keys)
                    .where(
                        idempotency_keys.c.scope == "electrolysis.normalise",
                        idempotency_keys.c.idempotency_key == key,
                    )
                ).scalar_one()
                == 0
            )
        with migrated.begin() as connection:
            classified, _unreachable = classify_objects(connection, object_store)
        assert (
            next(item.classification for item in classified if item.object_uri == pending_uri)
            == "diagnostic_orphan"
        )
    finally:
        with migrated.begin() as connection:
            connection.execute(
                text(f"DROP TRIGGER IF EXISTS {trigger_name} ON electrolysis_auxiliary_results")
            )
            connection.execute(text(f"DROP FUNCTION IF EXISTS {function_name}()"))

    stored = service.normalise(
        primary.artifact.source_artifact_id,
        profile.profile_id,
        idempotency_key=key,
    )
    assert stored.replayed is False
    with migrated.connect() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(normalised_electrolysis_observations)
                .where(
                    normalised_electrolysis_observations.c.observation_id
                    == stored.result.observation.observation_id
                )
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                select(func.count())
                .select_from(electrolysis_auxiliary_results)
                .where(
                    electrolysis_auxiliary_results.c.observation_id
                    == stored.result.observation.observation_id
                )
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                select(func.count())
                .select_from(idempotency_keys)
                .where(
                    idempotency_keys.c.scope == "electrolysis.normalise",
                    idempotency_keys.c.idempotency_key == key,
                )
            ).scalar_one()
            == 1
        )

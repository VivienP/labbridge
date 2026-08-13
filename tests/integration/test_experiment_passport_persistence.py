from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Never

import pytest
from sqlalchemy import Engine, delete, select

from labbridge.application.cv_ingestion import CVIngestionService
from labbridge.application.experiments import ExperimentService, UserAssertionCommand
from labbridge.application.source_intake import IntakeSource, SourceArtifactService
from labbridge.domain.cv import CVImportProfile
from labbridge.domain.experiments import AssertionValue, ExperimentVersionConflictError
from labbridge.evidence.experiment_package import verify_experiment_package
from labbridge.infrastructure.objectstore import ObjectStore
from labbridge.infrastructure.persistence.cv import PostgresCVRecordRepository
from labbridge.infrastructure.persistence.experiments import PostgresExperimentRepository
from labbridge.infrastructure.persistence.source_artifacts import PostgresSourceArtifactRepository
from labbridge.infrastructure.persistence.tables import (
    experiment_packages,
    experiment_passports,
    experiment_versions,
    experiments,
    metadata_assertions,
    storage_objects,
    validation_findings,
    validation_runs,
)
from labbridge.runtime.reconciliation import classify_objects

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
SUPERSEDING_VERSION = 2


class SimulatedPassportProcessStopError(RuntimeError):
    pass


class StopAfterOneUploadStore:
    def __init__(self, inner: ObjectStore) -> None:
        self._inner = inner
        self.bucket = inner.bucket
        self.uploaded_key: str | None = None

    def put_and_verify(self, key: str, data: bytes, *, media_type: str) -> Never:
        self._inner.put_and_verify(key, data, media_type=media_type)
        self.uploaded_key = key
        raise SimulatedPassportProcessStopError

    def get(self, key: str) -> bytes:
        return self._inner.get(key)

    def exists(self, key: str) -> bool:
        return self._inner.exists(key)


def test_postgres_and_minio_preserve_initial_and_superseding_releases(  # noqa: PLR0915
    migrated: Engine, object_store: ObjectStore
) -> None:
    now = datetime(2026, 8, 12, 20, 0, tzinfo=UTC)
    source_service = SourceArtifactService(
        PostgresSourceArtifactRepository(migrated), object_store, clock=lambda: now
    )
    source_bytes = (ROOT / "fixtures/source/synthetic-replay-cv-opaque.csv").read_bytes()
    source = source_service.intake(
        IntakeSource(
            intake_id=f"phase3-integration-{uuid.uuid4().hex}",
            data=source_bytes,
            filename="synthetic-replay-cv-opaque.csv",
            media_type="text/csv",
            data_origin="synthetic",
            execution_mode="replay",
        )
    )
    profile = CVImportProfile.model_validate_json(
        (ROOT / "fixtures/import-profiles/synthetic-replay-cv-v1.json").read_text(encoding="utf-8")
    ).model_copy(update={"environment_id": f"phase3_{uuid.uuid4().hex}"})
    cv_service = CVIngestionService(
        source_service,
        PostgresCVRecordRepository(migrated, object_store, clock=lambda: now),
        producing_version="0.1.0",
    )
    stored_profile = cv_service.create_profile(
        profile, idempotency_key=f"profile-{uuid.uuid4().hex}"
    )
    normalised = cv_service.normalise(
        source.artifact.source_artifact_id,
        stored_profile.profile_id,
        idempotency_key=f"normalise-{uuid.uuid4().hex}",
    )
    repository = PostgresExperimentRepository(migrated, object_store, clock=lambda: now)
    service = ExperimentService(
        cv_service,
        source_service,
        repository,
        clock=lambda: now,
        producing_versions={"labbridge": "0.1.0", "experiment_package": "1"},
    )
    created = service.create_experiment(
        normalised.result.observation.observation_id,
        expected_version=0,
        idempotency_key=f"experiment-{uuid.uuid4().hex}",
    )
    experiment_id = created.experiment.experiment_id
    passport_key = f"passport-{uuid.uuid4().hex}"
    stopping_store = StopAfterOneUploadStore(object_store)
    interrupted_service = ExperimentService(
        cv_service,
        source_service,
        PostgresExperimentRepository(migrated, stopping_store, clock=lambda: now),
        clock=lambda: now,
        producing_versions={"labbridge": "0.1.0", "experiment_package": "1"},
    )
    with pytest.raises(SimulatedPassportProcessStopError):
        interrupted_service.release_passport(
            experiment_id,
            expected_version=1,
            idempotency_key=passport_key,
        )
    assert stopping_store.uploaded_key is not None
    later = datetime(2026, 8, 12, 20, 5, tzinfo=UTC)
    service = ExperimentService(
        cv_service,
        source_service,
        PostgresExperimentRepository(migrated, object_store, clock=lambda: later),
        clock=lambda: later,
        producing_versions={"labbridge": "0.1.0", "experiment_package": "1"},
    )
    first_passport = service.release_passport(
        experiment_id,
        expected_version=1,
        idempotency_key=passport_key,
    )
    assert first_passport.passport.released_at == now
    package_key = f"package-{uuid.uuid4().hex}"
    stopping_package_store = StopAfterOneUploadStore(object_store)
    interrupted_package_service = ExperimentService(
        cv_service,
        source_service,
        PostgresExperimentRepository(migrated, stopping_package_store, clock=lambda: later),
        clock=lambda: later,
        producing_versions={"labbridge": "0.1.0", "experiment_package": "1"},
    )
    with pytest.raises(SimulatedPassportProcessStopError):
        interrupted_package_service.create_package(
            experiment_id,
            passport_id=first_passport.passport.passport_id,
            expected_version=1,
            idempotency_key=package_key,
        )
    assert stopping_package_store.uploaded_key is not None
    with migrated.begin() as connection:
        classified, unreachable = classify_objects(connection, object_store)
        pending_package_uri = connection.execute(
            select(storage_objects.c.object_uri).where(
                storage_objects.c.object_key == stopping_package_store.uploaded_key
            )
        ).scalar_one()
    assert not unreachable
    assert (
        next(item.classification for item in classified if item.object_uri == pending_package_uri)
        == "diagnostic_orphan"
    )
    first_package = service.create_package(
        experiment_id,
        passport_id=first_passport.passport.passport_id,
        expected_version=1,
        idempotency_key=package_key,
    )
    first_bytes = service.download_package(first_package.package.package_id)
    profile_assertion = next(
        item
        for item in created.experiment.assertions
        if item.field_name == "reference_scale" and item.origin == "user_supplied"
    )
    source_assertion = next(
        item
        for item in created.experiment.assertions
        if item.field_name == "source_artifact" and item.origin == "source_file"
    )
    service.add_user_assertion(
        experiment_id,
        expected_version=1,
        idempotency_key=f"assertion-{uuid.uuid4().hex}",
        command=UserAssertionCommand(
            field_name="reference_scale",
            requirement_class="conditional",
            transformation="none",
            value=AssertionValue(state="known", value="RHE"),
            evidence_note="Integration-test operator declaration.",
            supplements_assertion_id=profile_assertion.assertion_id,
        ),
    )
    second_passport = service.release_passport(
        experiment_id,
        expected_version=2,
        idempotency_key=f"passport-{uuid.uuid4().hex}",
    )
    second_package = service.create_package(
        experiment_id,
        passport_id=second_passport.passport.passport_id,
        expected_version=2,
        idempotency_key=f"package-{uuid.uuid4().hex}",
    )

    assert service.download_package(first_package.package.package_id) == first_bytes
    assert verify_experiment_package(first_bytes).experiment_version == 1
    assert (
        verify_experiment_package(
            service.download_package(second_package.package.package_id)
        ).experiment_version
        == SUPERSEDING_VERSION
    )
    release_uris: set[str] = set()
    with migrated.begin() as connection:
        for row in connection.execute(
            select(
                experiment_passports.c.json_object_uri,
                experiment_passports.c.html_object_uri,
            ).where(experiment_passports.c.experiment_id == experiment_id)
        ):
            release_uris.update((row.json_object_uri, row.html_object_uri))
        release_uris.update(
            connection.execute(
                select(experiment_packages.c.object_uri).where(
                    experiment_packages.c.experiment_id == experiment_id
                )
            )
            .scalars()
            .all()
        )
        classified, unreachable = classify_objects(connection, object_store)
    by_uri = {item.object_uri: item.classification for item in classified}
    assert not unreachable
    assert release_uris
    assert {by_uri[uri] for uri in release_uris} == {"accepted_evidence"}

    active_user = next(
        item
        for item in second_passport.passport.assertions
        if item.field_name == "reference_scale"
        and item.origin == "user_supplied"
        and item.value.state == "known"
    )

    def race_correction(value: str) -> str:
        try:
            service.add_user_assertion(
                experiment_id,
                expected_version=2,
                idempotency_key=f"concurrent-{value}-{uuid.uuid4().hex}",
                command=UserAssertionCommand(
                    field_name="reference_scale",
                    requirement_class="conditional",
                    transformation="none",
                    value=AssertionValue(state="known", value=value),
                    evidence_note=f"Concurrent integration declaration: {value}.",
                    supersedes_assertion_id=active_user.assertion_id,
                ),
            )
        except ExperimentVersionConflictError:
            return "conflict"
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(race_correction, ("RHE", "Ag/AgCl")))
    assert sorted(outcomes) == ["accepted", "conflict"]

    with pytest.raises(ExperimentVersionConflictError):
        repository.store_passport(
            second_passport.passport,
            expected_version=2,
            idempotency_key=f"stale-passport-{uuid.uuid4().hex}",
            request_hash="a" * 64,
        )
    with migrated.connect() as connection:
        stored_source = connection.execute(
            select(metadata_assertions.c.body).where(
                metadata_assertions.c.assertion_id == source_assertion.assertion_id
            )
        ).scalar_one()
        assert stored_source["origin"] == "source_file"
        assert stored_source["value"]["value"] == source.artifact.source_artifact_id

    with migrated.begin() as connection:
        connection.execute(
            delete(experiment_packages).where(experiment_packages.c.experiment_id == experiment_id)
        )
        connection.execute(
            delete(experiment_passports).where(
                experiment_passports.c.experiment_id == experiment_id
            )
        )
        validation_ids = select(validation_runs.c.validation_id).where(
            validation_runs.c.experiment_id == experiment_id
        )
        connection.execute(
            delete(validation_findings).where(
                validation_findings.c.validation_id.in_(validation_ids)
            )
        )
        connection.execute(
            delete(validation_runs).where(validation_runs.c.experiment_id == experiment_id)
        )
        connection.execute(
            delete(metadata_assertions).where(metadata_assertions.c.experiment_id == experiment_id)
        )
        connection.execute(
            delete(experiment_versions).where(experiment_versions.c.experiment_id == experiment_id)
        )
        connection.execute(delete(experiments).where(experiments.c.experiment_id == experiment_id))
        connection.execute(
            delete(storage_objects).where(storage_objects.c.object_uri.in_(release_uris))
        )

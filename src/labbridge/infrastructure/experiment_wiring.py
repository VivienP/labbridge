"""Construct the Experiment Passport service at adapter boundaries."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Engine, create_engine

from labbridge import __version__
from labbridge.application.cv_ingestion import (
    CVIngestionService,
    StoredNormalisation,
    StoredProfile,
)
from labbridge.application.electrolysis_ingestion import (
    ElectrolysisIngestionService,
    StoredElectrolysisNormalisation,
    StoredElectrolysisProfile,
)
from labbridge.application.experiments import ExperimentService
from labbridge.application.source_intake import SourceArtifactService

from .persistence.config import DatabaseSettings
from .persistence.experiments import PostgresExperimentRepository
from .source_wiring import build_source_store


class CombinedNormalisationReader:
    """Dispatch the two explicit observation identity namespaces without scientific inference."""

    def __init__(self, cv: CVIngestionService, electrolysis: ElectrolysisIngestionService) -> None:
        self._cv = cv
        self._electrolysis = electrolysis

    def get_normalisation(
        self, observation_id: str
    ) -> StoredNormalisation | StoredElectrolysisNormalisation:
        if observation_id.startswith("electrolysis-observation:"):
            return self._electrolysis.get_normalisation(observation_id)
        return self._cv.get_normalisation(observation_id)

    def get_profile(self, profile_id: str) -> StoredProfile | StoredElectrolysisProfile:
        if profile_id.startswith("electrolysis-profile:"):
            return self._electrolysis.get_profile(profile_id)
        return self._cv.get_profile(profile_id)


def build_experiment_service(
    source_service: SourceArtifactService,
    cv_service: CVIngestionService,
    engine: Engine | None = None,
    *,
    electrolysis_service: ElectrolysisIngestionService | None = None,
) -> ExperimentService:
    database = engine or create_engine(DatabaseSettings().dsn, future=True)
    store = build_source_store()
    return ExperimentService(
        (
            cv_service
            if electrolysis_service is None
            else CombinedNormalisationReader(cv_service, electrolysis_service)
        ),
        source_service,
        PostgresExperimentRepository(database, store, clock=lambda: datetime.now(UTC)),
        clock=lambda: datetime.now(UTC),
        producing_versions={"labbridge": __version__, "experiment_package": "1"},
    )


__all__ = ["CombinedNormalisationReader", "build_experiment_service"]

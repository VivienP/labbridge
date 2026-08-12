"""Construct the Experiment Passport service at adapter boundaries."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Engine, create_engine

from labbridge import __version__
from labbridge.application.cv_ingestion import CVIngestionService
from labbridge.application.experiments import ExperimentService
from labbridge.application.source_intake import SourceArtifactService

from .persistence.config import DatabaseSettings
from .persistence.experiments import PostgresExperimentRepository
from .source_wiring import build_source_store


def build_experiment_service(
    source_service: SourceArtifactService,
    cv_service: CVIngestionService,
    engine: Engine | None = None,
) -> ExperimentService:
    database = engine or create_engine(DatabaseSettings().dsn, future=True)
    store = build_source_store()
    return ExperimentService(
        cv_service,
        source_service,
        PostgresExperimentRepository(database, store, clock=lambda: datetime.now(UTC)),
        clock=lambda: datetime.now(UTC),
        producing_versions={"labbridge": __version__, "experiment_package": "1"},
    )


__all__ = ["build_experiment_service"]

"""Construct the galvanostatic-electrolysis ingestion service at adapter boundaries."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Engine, create_engine

from labbridge import __version__
from labbridge.application.electrolysis_ingestion import ElectrolysisIngestionService
from labbridge.application.source_intake import SourceArtifactService

from .persistence.config import DatabaseSettings
from .persistence.electrolysis import PostgresElectrolysisRecordRepository
from .source_wiring import build_source_store


def build_electrolysis_service(
    source_service: SourceArtifactService, engine: Engine | None = None
) -> ElectrolysisIngestionService:
    database = engine or create_engine(DatabaseSettings().dsn, future=True)
    store = build_source_store()
    return ElectrolysisIngestionService(
        source_service,
        PostgresElectrolysisRecordRepository(database, store, clock=lambda: datetime.now(UTC)),
        producing_version=__version__,
    )


__all__ = ["build_electrolysis_service"]

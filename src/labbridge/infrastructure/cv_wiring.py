"""Construct the generic CV ingestion service at adapter boundaries."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Engine, create_engine

from labbridge import __version__
from labbridge.application.cv_ingestion import CVIngestionService
from labbridge.application.source_intake import SourceArtifactService

from .persistence.config import DatabaseSettings
from .persistence.cv import PostgresCVRecordRepository
from .source_wiring import build_source_store


def build_cv_service(
    source_service: SourceArtifactService, engine: Engine | None = None
) -> CVIngestionService:
    database = engine or create_engine(DatabaseSettings().dsn, future=True)
    store = build_source_store()
    return CVIngestionService(
        source_service,
        PostgresCVRecordRepository(database, store, clock=lambda: datetime.now(UTC)),
        producing_version=__version__,
    )


__all__ = ["build_cv_service"]

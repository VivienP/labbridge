"""Construct the concrete source-intake service at adapter boundaries."""

from __future__ import annotations

from datetime import UTC, datetime

import boto3
from botocore.config import Config as BotoConfig
from sqlalchemy import Engine, create_engine

from labbridge.application.source_intake import SourceArtifactService

from .objectstore import S3ObjectStore
from .persistence.config import DatabaseSettings, ObjectStoreSettings
from .persistence.source_artifacts import PostgresSourceArtifactRepository


def build_source_store() -> S3ObjectStore:
    settings = ObjectStoreSettings()
    client = boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key,
        aws_secret_access_key=settings.secret_key,
        config=BotoConfig(signature_version="s3v4"),
        region_name=settings.region,
    )
    store = S3ObjectStore(client, bucket=settings.bucket)
    store.ensure_bucket()
    return store


def build_source_service(engine: Engine | None = None) -> SourceArtifactService:
    database = engine or create_engine(DatabaseSettings().dsn, future=True)
    store = build_source_store()
    return SourceArtifactService(
        PostgresSourceArtifactRepository(database),
        store,
        clock=lambda: datetime.now(UTC),
    )


__all__ = ["build_source_service", "build_source_store"]

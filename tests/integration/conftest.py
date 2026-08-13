"""Fixtures for tests that need a real PostgreSQL.

`AI_CONTRACT.md` §9: a test that mocks away the database transaction does not prove the
corresponding operational guarantee. Everything here therefore talks to the database in
`docker-compose.yml`, and skips loudly when it is not running rather than passing vacuously.

Each test runs inside a transaction that is rolled back, so the suite is order-independent and
leaves no rows behind. The schema is migrated once per session — through Alembic, never through
`metadata.create_all`, because creating tables from the model would test a schema no deployment ever
runs (`AI_CONTRACT.md` §10).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final

import boto3
import pytest
from alembic import command
from alembic.config import Config
from botocore.config import Config as BotoConfig
from sqlalchemy import Connection, Engine, create_engine, delete, select, text

from labbridge.infrastructure.objectstore import S3ObjectStore
from labbridge.infrastructure.persistence.config import DatabaseSettings, ObjectStoreSettings
from labbridge.infrastructure.persistence.tables import (
    attempt_outcomes,
    attempts,
    budget_ledger,
    campaigns,
    derived_metrics,
    events,
    idempotency_keys,
    jobs,
    observations,
    storage_objects,
    work_items,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return config


@pytest.fixture(scope="session")
def alembic_config() -> Config:
    return _alembic_config()


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    settings = DatabaseSettings()
    engine = create_engine(settings.dsn, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("select 1"))
    except Exception as error:
        pytest.skip(
            f"PostgreSQL unreachable at {settings.host}:{settings.port} ({error}). "
            "Start it with `docker compose up -d`."
        )
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def migrated(engine: Engine) -> Engine:
    """The schema at head, applied by Alembic exactly as a deployment would apply it."""
    command.upgrade(_alembic_config(), "head")
    return engine


@pytest.fixture(scope="session")
def object_store() -> Iterator[S3ObjectStore]:
    """A real S3-compatible store. Skips rather than passing vacuously when MinIO is not up."""
    settings = ObjectStoreSettings()
    client = boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key,
        aws_secret_access_key=settings.secret_key,
        config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 1}),
        region_name=settings.region,
    )
    store = S3ObjectStore(client, bucket=f"{settings.bucket}-tests")
    try:
        store.ensure_bucket()
    except Exception as error:
        pytest.skip(
            f"MinIO unreachable at {settings.endpoint_url} ({error}). "
            "Start it with `docker compose up -d`."
        )
    yield store


@pytest.fixture
def connection(migrated: Engine) -> Iterator[Connection]:
    """A connection whose transaction is always rolled back, so tests cannot leak state."""
    with migrated.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


#: Children before parents. Every RESTRICT in the schema is deliberate, so a teardown that ignored
#: them would fail loudly rather than cascade evidence away — and the order lives here once, because
#: three copies of it is how adding `budget_ledger` broke three test files at the same time.
_PURGE_ORDER: Final = (
    (derived_metrics, "observation"),
    (attempt_outcomes, "campaign"),
    (observations, "campaign"),
    # Before `attempts`: a staged object names the attempt that wrote it, under `RESTRICT`, so the
    # attempt cannot go first. That reference is the whole point — it is what lets reconciliation
    # attribute an orphan to an execution.
    (storage_objects, "work_item"),
    # Settlements name attempts and reservations name jobs, both under `RESTRICT`.
    (budget_ledger, "campaign"),
    (attempts, "work_item"),
    (jobs, "work_item"),
    (events, "campaign"),
    (work_items, "campaign"),
    # Before the campaign it references. The foreign key is `DEFERRABLE INITIALLY DEFERRED`, but
    # that defers only the insert side: `ON DELETE RESTRICT` is checked at the delete itself, so a
    # campaign removed while an idempotency record still names it fails there, not at commit.
    (idempotency_keys, "campaign"),
    (campaigns, "self"),
)


@pytest.fixture
def purge_campaign() -> Callable[[Connection, uuid.UUID], None]:
    """Injected rather than imported: `tests/` is not a package, so a relative import from a
    conftest does not resolve."""
    return _purge_campaign


def _purge_campaign(connection: Connection, campaign_id: uuid.UUID) -> None:
    """Delete everything one campaign owns, in foreign-key order."""
    owned_items = select(work_items.c.work_item_id).where(work_items.c.campaign_id == campaign_id)
    owned_observations = select(observations.c.observation_id).where(
        observations.c.campaign_id == campaign_id
    )
    for table, scope in _PURGE_ORDER:
        if scope == "campaign":
            connection.execute(delete(table).where(table.c.campaign_id == campaign_id))
        elif scope == "work_item":
            connection.execute(delete(table).where(table.c.work_item_id.in_(owned_items)))
        elif scope == "observation":
            connection.execute(delete(table).where(table.c.observation_id.in_(owned_observations)))
        else:
            connection.execute(delete(table).where(table.c.campaign_id == campaign_id))

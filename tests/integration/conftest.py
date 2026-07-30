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

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text

from labbridge.infrastructure.persistence.config import DatabaseSettings

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


@pytest.fixture
def connection(migrated: Engine) -> Iterator[Connection]:
    """A connection whose transaction is always rolled back, so tests cannot leak state."""
    with migrated.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()

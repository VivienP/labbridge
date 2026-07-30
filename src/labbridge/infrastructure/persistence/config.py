"""Database and object-storage settings, read from the environment.

`AI_CONTRACT.md` §11 forbids writing secrets into the repository, so nothing here holds a real
credential. Defaults point at the local `docker-compose.yml`, whose credentials are development-only
literals; any deployed environment supplies its own through `LABBRIDGE_*` variables.

The compose ports are non-standard (55432, 59000) on purpose: a test that silently connects to a
PostgreSQL already running on 5432 proves nothing about this project's schema.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LABBRIDGE_DB_", extra="ignore")

    host: str = "localhost"
    port: int = 55432
    user: str = "labbridge"
    password: str = "labbridge"
    name: str = "labbridge"

    @property
    def dsn(self) -> str:
        """A SQLAlchemy URL. `psycopg` names the driver rather than relying on a default."""
        return (
            f"postgresql+psycopg://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"
        )


class ObjectStoreSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LABBRIDGE_S3_", extra="ignore")

    endpoint_url: str | None = "http://localhost:59000"
    access_key: str = "labbridge"
    secret_key: str = "labbridge-dev-only"
    region: str = "us-east-1"
    bucket: str = Field(default="labbridge", min_length=3)

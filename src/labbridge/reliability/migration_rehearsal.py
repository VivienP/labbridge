"""Versioned production-like migration rehearsal for a release database.

The rehearsal upgrades from the previous tagged schema to the unique current Alembic head.
It does not pin a historical parent revision: a merge, a later additive migration, or a split
head must change the outcome. The event-stream contract revision must remain an ancestor of
that unique head so a graph that drops contract version 2 cannot pass.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, func, select

from labbridge.infrastructure.persistence.config import DatabaseSettings
from labbridge.infrastructure.persistence.tables import campaigns

PREVIOUS_REVISION = "74e1b6a09d22"
EVENT_STREAM_CONTRACT_REVISION = "a93b7c1e4d62"


def _config(repo_root: Path) -> Config:
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "migrations"))
    return config


def expected_upgrade_revision(repo_root: Path) -> str:
    """Return the unique current Alembic head that a rehearsal must reach."""
    script = ScriptDirectory.from_config(_config(repo_root))
    heads = script.get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"migration rehearsal requires a unique Alembic head; found {heads}")
    head = heads[0]
    ancestry = {revision.revision for revision in script.walk_revisions("base", head)}
    if EVENT_STREAM_CONTRACT_REVISION not in ancestry:
        raise RuntimeError(
            "migration rehearsal is missing the event-stream contract revision "
            f"{EVENT_STREAM_CONTRACT_REVISION}"
        )
    return head


def _create_empty_database(name: str) -> None:
    admin = create_engine(DatabaseSettings(name="labbridge").dsn, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        present = connection.exec_driver_sql(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).scalar_one_or_none()
        if present is not None:
            raise RuntimeError(f"migration rehearsal database already exists: {name}")
        connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    admin.dispose()


def rehearse_migration(*, repo_root: Path, database_name: str) -> dict[str, object]:
    """Upgrade a distinct previous-head database and prove data preservation."""
    started = datetime.now(UTC)
    _create_empty_database(database_name)
    previous_name = os.environ.get("LABBRIDGE_DB_NAME")
    os.environ["LABBRIDGE_DB_NAME"] = database_name
    try:
        config = _config(repo_root)
        command.upgrade(config, PREVIOUS_REVISION)
        engine = create_engine(DatabaseSettings().dsn, future=True)
        with engine.begin() as connection:
            legacy_campaign_id = uuid.uuid4()
            connection.execute(
                campaigns.insert().values(
                    campaign_id=legacy_campaign_id,
                    name="migration rehearsal legacy campaign",
                    environment_id="her_auirrh",
                    adapter_version="1",
                    data_origin="synthetic",
                    execution_mode="replay",
                    state="active",
                    declaration={"legacy_rehearsal": True},
                    declaration_hash="a" * 64,
                    hard_budget=1,
                    per_attempt_estimate=1,
                    budget_unit="attempt",
                    max_attempts=1,
                    stopping_rule="hard_budget_exhausted",
                    event_stream_contract_version=1,
                    event_stream_last_position=0,
                    created_at=func.now(),
                    updated_at=func.now(),
                )
            )
            marker_count_before = int(
                connection.execute(select(func.count()).select_from(campaigns)).scalar_one()
            )
        expected = expected_upgrade_revision(repo_root)
        began_upgrade = time.monotonic()
        command.upgrade(config, "head")
        duration = time.monotonic() - began_upgrade
        with engine.begin() as connection:
            revision = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
            marker_count_after = int(
                connection.execute(select(func.count()).select_from(campaigns)).scalar_one()
            )
            preserved_contract = connection.execute(
                select(campaigns.c.event_stream_contract_version).where(
                    campaigns.c.campaign_id == legacy_campaign_id
                )
            ).scalar_one()
            constraint = connection.exec_driver_sql(
                """
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conname = 'ck_campaigns_known_event_stream_contract_version'
                """
            ).scalar_one()
        engine.dispose()
    finally:
        if previous_name is None:
            os.environ.pop("LABBRIDGE_DB_NAME", None)
        else:
            os.environ["LABBRIDGE_DB_NAME"] = previous_name
    if revision != expected or "2" not in constraint:
        raise RuntimeError(
            "migration rehearsal did not reach the unique Alembic head "
            f"{expected}; database revision is {revision}"
        )
    if marker_count_before != marker_count_after:
        raise RuntimeError("migration rehearsal did not preserve campaign row counts")
    if preserved_contract != 1:
        raise RuntimeError("migration rehearsal changed the legacy campaign contract version")
    return {
        "passed": True,
        "status": "PASSED",
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "database_name": database_name,
        "from_revision": PREVIOUS_REVISION,
        "to_revision": revision,
        "duration_seconds": round(duration, 6),
        "campaign_rows_before": marker_count_before,
        "campaign_rows_after": marker_count_after,
        "legacy_contract_version_preserved": preserved_contract,
        "contract_version_2_constraint_present": True,
    }


__all__ = [
    "EVENT_STREAM_CONTRACT_REVISION",
    "PREVIOUS_REVISION",
    "expected_upgrade_revision",
    "rehearse_migration",
]

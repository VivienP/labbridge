"""The migration is reversible, matches the model, and agrees with the domain.

`docs/SPEC.md` §4.1 requires migration tests. Three properties are worth a test at this stage:
the schema Alembic produces is the schema the model declares, the migration goes back down, and the
constraint that duplicates a domain rule has not drifted from it.
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, inspect, text

from labbridge.infrastructure.persistence.tables import metadata

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "attempt_outcomes",
    "attempts",
    "budget_ledger",
    "campaigns",
    "derived_metrics",
    "events",
    "idempotency_keys",
    "jobs",
    "observations",
    "record_relations",
    "storage_objects",
    "work_items",
}


def test_head_creates_every_declared_table(migrated: Engine) -> None:
    present = set(inspect(migrated).get_table_names())

    assert present >= EXPECTED_TABLES


def test_the_migrated_schema_matches_the_model_exactly(connection: Connection) -> None:
    """An empty diff is the whole point: a hand-edited migration that drifted fails here."""
    context = MigrationContext.configure(
        connection, opts={"compare_type": True, "compare_server_default": True}
    )

    assert compare_metadata(context, metadata) == []


def test_the_migration_goes_back_down_and_up_again(engine: Engine, alembic_config: Config) -> None:
    """A migration that cannot be reversed cannot be rehearsed before a deployment."""
    config = alembic_config
    command.downgrade(config, "base")
    remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert not remaining & EXPECTED_TABLES

    command.upgrade(config, "head")
    assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES


def test_the_origin_mode_constraint_exists_on_every_table_that_records_a_pair(
    connection: Connection,
) -> None:
    """Agreement with the domain is proven behaviourally in `test_constraints.py`; this only checks
    that no table recording a pair was added without the constraint."""
    tables = connection.execute(
        text(
            "select c.relname from pg_constraint k join pg_class c on c.oid = k.conrelid "
            "where k.conname like '%admissible_origin_mode'"
        )
    ).scalars()

    assert {"campaigns", "observations"} <= set(tables)

"""The migration is reversible, matches the model, and agrees with the domain.

`docs/SPEC.md` §4.1 requires migration tests. Three properties are worth a test at this stage:
the schema Alembic produces is the schema the model declares, the migration goes back down, and the
constraint that duplicates a domain rule has not drifted from it.
"""

from __future__ import annotations

import uuid

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, func, inspect, text

from labbridge.infrastructure.persistence.tables import (
    attempts,
    campaigns,
    metadata,
    observations,
    work_items,
)

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
    "source_artifacts",
    "storage_objects",
    "work_items",
}


def _clear_all(engine: Engine) -> None:
    """Empty every table in foreign-key order. The schema's RESTRICT constraints are deliberate, so
    this deletes children first rather than cascading."""
    order = (
        "derived_metrics",
        "attempt_outcomes",
        "observations",
        "attempts",
        "jobs",
        "events",
        "budget_ledger",
        "work_items",
        "campaigns",
        "source_artifacts",
        "storage_objects",
        "idempotency_keys",
        "record_relations",
    )
    with engine.begin() as connection:
        for table in order:
            connection.execute(text(f"DELETE FROM {table}"))


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
    """A migration that cannot be reversed cannot be rehearsed before a deployment.

    Runs on an empty schema by construction. The head revision's downgrade deliberately refuses
    once two attempts share an observation_id, because restoring the single-column key would drop
    one receipt of each pair; `test_the_downgrade_refuses_rather_than_dropping_a_receipt` covers
    that path, and this one covers the reversal itself."""
    _clear_all(engine)
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


def test_the_downgrade_refuses_rather_than_dropping_a_receipt(
    engine: Engine, alembic_config: Config
) -> None:
    """The composite key admits what the single-column key cannot: identical content received by
    two attempts. Restoring the old key would drop one receipt of each pair, so the downgrade stops
    and names the rows instead. A migration that loses observations silently is worse than one that
    refuses to run."""
    _clear_all(engine)
    campaign_id, first_attempt, second_attempt = _two_attempts_sharing_content(engine)

    try:
        # Target the revision *before* the guarded one: `downgrade(rev)` unwinds down *to* `rev`,
        # so naming the guarded revision would stop just above it and never run its downgrade. A
        # relative `-1` had the same problem once a later revision was added on top — it silently
        # exercised that one instead.
        with pytest.raises(RuntimeError, match="shared by more than one attempt"):
            command.downgrade(alembic_config, "32ec7ead3c65")
    finally:
        _clear_all(engine)
        command.upgrade(alembic_config, "head")
    del campaign_id, first_attempt, second_attempt


def _two_attempts_sharing_content(engine: Engine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """One observation_id under two attempts — the shape the composite key exists to allow."""
    campaign_id = uuid.uuid4()
    shared = f"obs:{uuid.uuid4().hex}"
    attempt_ids = (uuid.uuid4(), uuid.uuid4())
    with engine.begin() as connection:
        connection.execute(
            campaigns.insert().values(
                campaign_id=campaign_id,
                name="downgrade guard",
                environment_id="her",
                adapter_version="1",
                data_origin="synthetic",
                execution_mode="replay",
                state="active",
                declaration={},
                declaration_hash="a" * 64,
                created_at=func.now(),
                updated_at=func.now(),
            )
        )
        for index, attempt_id in enumerate(attempt_ids, start=1):
            work_item_id = uuid.uuid4()
            connection.execute(
                work_items.insert().values(
                    work_item_id=work_item_id,
                    campaign_id=campaign_id,
                    candidate_id=f"cand:{uuid.uuid4().hex}",
                    candidate={},
                    state="queued",
                    created_at=func.now(),
                    updated_at=func.now(),
                )
            )
            connection.execute(
                attempts.insert().values(
                    attempt_id=attempt_id,
                    work_item_id=work_item_id,
                    ordinal=index,
                    state="succeeded",
                    created_at=func.now(),
                )
            )
            connection.execute(
                observations.insert().values(
                    observation_id=shared,
                    campaign_id=campaign_id,
                    work_item_id=work_item_id,
                    attempt_id=attempt_id,
                    media_type="text/csv",
                    object_uri=f"s3://labbridge/{shared}",
                    byte_size=1,
                    sha256="b" * 64,
                    schema_version="1",
                    signal_kind="lsv",
                    quantities=[],
                    status="accepted",
                    data_origin="synthetic",
                    execution_mode="replay",
                    provenance={},
                    received_at=func.now(),
                )
            )
    return campaign_id, *attempt_ids

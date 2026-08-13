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
from sqlalchemy import Connection, Engine, func, inspect, select, text

from labbridge.infrastructure.persistence.tables import (
    attempts,
    budget_ledger,
    campaigns,
    idempotency_keys,
    jobs,
    metadata,
    observations,
    work_items,
)
from labbridge.runtime.events import IncompleteEventStreamError, load_replay_stream

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "attempt_outcomes",
    "attempts",
    "budget_ledger",
    "campaigns",
    "cv_structural_findings",
    "cv_transformation_records",
    "cv_parser_records",
    "derived_metrics",
    "events",
    "experiment_packages",
    "experiment_passports",
    "experiment_versions",
    "experiments",
    "idempotency_keys",
    "import_profiles",
    "jobs",
    "observations",
    "normalised_cv_observations",
    "metadata_assertions",
    "record_relations",
    "source_artifacts",
    "storage_objects",
    "validation_findings",
    "validation_runs",
    "work_items",
}
DEFAULT_MAX_ATTEMPTS = 3


def _clear_all(engine: Engine) -> None:
    """Empty every table in foreign-key order. The schema's RESTRICT constraints are deliberate, so
    this deletes children first rather than cascading."""
    present = set(inspect(engine).get_table_names())
    order = (
        "experiment_packages",
        "experiment_passports",
        "validation_findings",
        "validation_runs",
        "metadata_assertions",
        "experiment_versions",
        "experiments",
        "cv_transformation_records",
        "cv_structural_findings",
        "cv_parser_records",
        "derived_metrics",
        "attempt_outcomes",
        "observations",
        "normalised_cv_observations",
        "import_profiles",
        "source_artifacts",
        # Before `attempts` and `work_items`: a staged object references both under `RESTRICT`.
        "storage_objects",
        # Reservations and settlements reference attempts and jobs under `RESTRICT`.
        "budget_ledger",
        "attempts",
        "jobs",
        "events",
        "work_items",
        # Before `campaigns`: an idempotency record names the campaign it produced, under
        # `RESTRICT`, which PostgreSQL checks at the delete rather than at commit.
        "idempotency_keys",
        "campaigns",
        "record_relations",
    )
    with engine.begin() as connection:
        for table in order:
            if table in present:
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


def test_existing_campaigns_are_marked_legacy_without_inventing_events(
    engine: Engine, alembic_config: Config
) -> None:
    _clear_all(engine)
    campaign_id = uuid.uuid4()
    event_id = uuid.uuid4()
    try:
        command.downgrade(alembic_config, "1e6a158aabea")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO campaigns "
                    "(campaign_id,name,environment_id,adapter_version,data_origin,execution_mode,"
                    "state,declaration,declaration_hash,created_at,updated_at) VALUES "
                    "(:campaign_id,'legacy','her','1','synthetic','replay','active','{}',:digest,"
                    "now(),now())"
                ),
                {"campaign_id": campaign_id, "digest": "a" * 64},
            )
            connection.execute(
                text(
                    "INSERT INTO events "
                    "(event_id,campaign_id,aggregate_id,aggregate_type,sequence,event_type,"
                    "schema_version,occurred_at,recorded_at,correlation_id,payload) VALUES "
                    "(:event_id,:campaign_id,:campaign_id,'campaign',1,'campaign.declared',1,"
                    "now(),now(),:correlation_id,'{}')"
                ),
                {
                    "event_id": event_id,
                    "campaign_id": campaign_id,
                    "correlation_id": uuid.uuid4(),
                },
            )

        command.upgrade(alembic_config, "head")
        with engine.begin() as connection:
            campaign = connection.execute(
                select(
                    campaigns.c.event_stream_contract_version,
                    campaigns.c.event_stream_last_position,
                ).where(campaigns.c.campaign_id == campaign_id)
            ).one()
            assert campaign.event_stream_contract_version == 0
            assert campaign.event_stream_last_position == 1
            assert (
                connection.execute(
                    text("SELECT count(*) FROM events WHERE campaign_id = :campaign_id"),
                    {"campaign_id": campaign_id},
                ).scalar_one()
                == 1
            )
            with pytest.raises(IncompleteEventStreamError):
                load_replay_stream(connection, campaign_id)
    finally:
        _clear_all(engine)
        command.upgrade(alembic_config, "head")


def test_contract_downgrade_refuses_complete_campaigns(
    engine: Engine, alembic_config: Config
) -> None:
    _clear_all(engine)
    campaign_id = uuid.uuid4()
    try:
        with engine.begin() as connection:
            connection.execute(
                campaigns.insert().values(
                    campaign_id=campaign_id,
                    name="complete",
                    environment_id="her",
                    adapter_version="1",
                    data_origin="synthetic",
                    execution_mode="replay",
                    state="active",
                    declaration={},
                    declaration_hash="a" * 64,
                    event_stream_contract_version=1,
                    event_stream_last_position=0,
                    created_at=func.now(),
                    updated_at=func.now(),
                )
            )
        with pytest.raises(RuntimeError, match="complete campaigns exist"):
            command.downgrade(alembic_config, "1e6a158aabea")
    finally:
        _clear_all(engine)
        command.upgrade(alembic_config, "head")


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


def test_the_scope_downgrade_refuses_rather_than_collapsing_two_scopes(
    engine: Engine, alembic_config: Config
) -> None:
    """The composite key admits what the single-column key cannot: one caller-chosen token used by
    two operations. Restoring the narrow key would have to drop one of them, so the downgrade stops
    and says why instead."""
    _clear_all(engine)
    shared = f"key:{uuid.uuid4().hex}"
    with engine.begin() as connection:
        for scope in ("campaigns.create", "campaigns.cancel"):
            connection.execute(
                idempotency_keys.insert().values(
                    scope=scope,
                    idempotency_key=shared,
                    request_hash="c" * 64,
                    campaign_id=None,
                    response={"work_items": 0},
                    created_at=func.now(),
                )
            )

    try:
        # Target the revision *before* the guarded one: `downgrade(rev)` unwinds down *to* `rev`, so
        # naming the guarded revision would stop just above it and never run its downgrade.
        with pytest.raises(RuntimeError, match="used in several scopes"):
            command.downgrade(alembic_config, "8c4d7e2a91bf")
    finally:
        _clear_all(engine)
        command.upgrade(alembic_config, "head")


def test_the_scope_downgrade_proceeds_when_no_key_is_shared(
    engine: Engine, alembic_config: Config
) -> None:
    """The guard must refuse a real collision and nothing else, or it is just a broken downgrade."""
    _clear_all(engine)
    with engine.begin() as connection:
        connection.execute(
            idempotency_keys.insert().values(
                scope="campaigns.create",
                idempotency_key=f"key:{uuid.uuid4().hex}",
                request_hash="d" * 64,
                campaign_id=None,
                response={"work_items": 0},
                created_at=func.now(),
            )
        )

    try:
        command.downgrade(alembic_config, "8c4d7e2a91bf")
        assert "campaign_id" not in {
            column["name"] for column in inspect(engine).get_columns("idempotency_keys")
        }
    finally:
        _clear_all(engine)
        command.upgrade(alembic_config, "head")


def test_an_existing_idempotency_record_is_resolved_to_the_campaign_it_returned(
    engine: Engine, alembic_config: Config
) -> None:
    """The backfill moves a value, it does not derive one: the campaign identifier it lifts out of
    `response` is the one the endpoint already returned to the caller.

    A record whose campaign is gone keeps a null rather than acquiring a dangling reference — the
    foreign key added by the same migration would refuse it, and inventing a campaign to satisfy it
    would be worse."""
    _clear_all(engine)
    campaign_id = uuid.uuid4()
    resolved = f"key:{uuid.uuid4().hex}"
    orphaned = f"key:{uuid.uuid4().hex}"
    try:
        command.downgrade(alembic_config, "8c4d7e2a91bf")
        with engine.begin() as connection:
            connection.execute(
                campaigns.insert().values(
                    campaign_id=campaign_id,
                    name="backfill",
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
            for key, named in ((resolved, str(campaign_id)), (orphaned, str(uuid.uuid4()))):
                connection.execute(
                    text(
                        "INSERT INTO idempotency_keys "
                        "(idempotency_key,scope,request_hash,response,created_at) VALUES "
                        "(:key,'campaigns.create',:digest,CAST(:response AS jsonb),now())"
                    ),
                    {
                        "key": key,
                        "digest": "b" * 64,
                        "response": f'{{"campaign_id": "{named}", "work_items": 2}}',
                    },
                )

        command.upgrade(alembic_config, "head")
        with engine.begin() as connection:
            backfilled = dict(
                connection.execute(
                    select(
                        idempotency_keys.c.idempotency_key, idempotency_keys.c.campaign_id
                    ).where(idempotency_keys.c.idempotency_key.in_((resolved, orphaned)))
                ).all()
            )
        assert backfilled == {resolved: campaign_id, orphaned: None}
    finally:
        _clear_all(engine)
        command.upgrade(alembic_config, "head")


def test_budget_reservation_migration_preserves_existing_campaign_and_ledger_rows(
    engine: Engine, alembic_config: Config
) -> None:
    _clear_all(engine)
    campaign_id = uuid.uuid4()
    entry_id = uuid.uuid4()
    try:
        command.downgrade(alembic_config, "61d3f47b809a")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO campaigns "
                    "(campaign_id,name,environment_id,adapter_version,data_origin,execution_mode,"
                    "state,declaration,declaration_hash,event_stream_contract_version,"
                    "event_stream_last_position,created_at,updated_at) VALUES "
                    "(:campaign_id,'legacy budget','her','1','synthetic','replay','active','{}',"
                    ":digest,1,0,now(),now())"
                ),
                {"campaign_id": campaign_id, "digest": "a" * 64},
            )
            connection.execute(
                text(
                    "INSERT INTO budget_ledger "
                    "(entry_id,campaign_id,kind,amount,unit,reason,recorded_at) VALUES "
                    "(:entry_id,:campaign_id,'consumed',0,'','legacy outcome',now())"
                ),
                {"entry_id": entry_id, "campaign_id": campaign_id},
            )

        command.upgrade(alembic_config, "head")
        with engine.begin() as connection:
            campaign = connection.execute(
                select(
                    campaigns.c.hard_budget,
                    campaigns.c.per_attempt_estimate,
                    campaigns.c.budget_unit,
                    campaigns.c.max_attempts,
                    campaigns.c.stopping_rule,
                ).where(campaigns.c.campaign_id == campaign_id)
            ).one()
            ledger = connection.execute(
                select(budget_ledger).where(budget_ledger.c.entry_id == entry_id)
            ).one()

        assert campaign.hard_budget > campaign.per_attempt_estimate
        assert campaign.budget_unit == "attempt"
        assert campaign.max_attempts == DEFAULT_MAX_ATTEMPTS
        assert campaign.stopping_rule == "hard_budget_exhausted"
        assert ledger.kind == "consumed"
        assert ledger.amount == 0
        assert ledger.unit == ""
        assert ledger.reservation_entry_id is None
    finally:
        _clear_all(engine)
        command.upgrade(alembic_config, "head")


def test_budget_reservation_migration_downgrade_keeps_legacy_rows(
    engine: Engine, alembic_config: Config
) -> None:
    _clear_all(engine)
    campaign_id = uuid.uuid4()
    entry_id = uuid.uuid4()
    try:
        with engine.begin() as connection:
            connection.execute(
                campaigns.insert().values(
                    campaign_id=campaign_id,
                    name="downgrade budget",
                    environment_id="her",
                    adapter_version="1",
                    data_origin="synthetic",
                    execution_mode="replay",
                    state="active",
                    declaration={},
                    declaration_hash="d" * 64,
                    hard_budget=10,
                    per_attempt_estimate=1,
                    budget_unit="attempt",
                    max_attempts=3,
                    stopping_rule="hard_budget_exhausted",
                    event_stream_contract_version=1,
                    event_stream_last_position=0,
                    created_at=func.now(),
                    updated_at=func.now(),
                )
            )
            connection.execute(
                budget_ledger.insert().values(
                    entry_id=entry_id,
                    campaign_id=campaign_id,
                    kind="consumed",
                    amount=1,
                    unit="attempt",
                    reason="legacy-compatible settlement",
                    recorded_at=func.now(),
                )
            )

        command.downgrade(alembic_config, "61d3f47b809a")
        columns = {column["name"] for column in inspect(engine).get_columns("campaigns")}
        with engine.begin() as connection:
            campaign_count = connection.execute(
                text("SELECT count(*) FROM campaigns WHERE campaign_id = :campaign_id"),
                {"campaign_id": campaign_id},
            ).scalar_one()
            entry_count = connection.execute(
                text("SELECT count(*) FROM budget_ledger WHERE entry_id = :entry_id"),
                {"entry_id": entry_id},
            ).scalar_one()

        assert "hard_budget" not in columns
        assert campaign_count == 1
        assert entry_count == 1
    finally:
        _clear_all(engine)
        command.upgrade(alembic_config, "head")


def test_budget_reservation_downgrade_preserves_linked_append_only_rows(
    engine: Engine, alembic_config: Config
) -> None:
    _clear_all(engine)
    campaign_id = uuid.uuid4()
    work_item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    settlement_id = uuid.uuid4()
    try:
        with engine.begin() as connection:
            connection.execute(
                campaigns.insert().values(
                    campaign_id=campaign_id,
                    name="linked downgrade budget",
                    environment_id="her",
                    adapter_version="1",
                    data_origin="synthetic",
                    execution_mode="replay",
                    state="active",
                    declaration={},
                    declaration_hash="e" * 64,
                    hard_budget=10,
                    per_attempt_estimate=1,
                    budget_unit="attempt",
                    max_attempts=3,
                    stopping_rule="hard_budget_exhausted",
                    event_stream_contract_version=1,
                    event_stream_last_position=0,
                    created_at=func.now(),
                    updated_at=func.now(),
                )
            )
            connection.execute(
                work_items.insert().values(
                    work_item_id=work_item_id,
                    campaign_id=campaign_id,
                    candidate_id="candidate:linked-downgrade",
                    candidate={"kind": "migration-test"},
                    state="accepted",
                    created_at=func.now(),
                    updated_at=func.now(),
                )
            )
            connection.execute(
                jobs.insert().values(
                    job_id=job_id,
                    work_item_id=work_item_id,
                    state="succeeded",
                    available_at=func.now(),
                    lease_generation=1,
                    attempt_count=1,
                    max_attempts=3,
                    command_version="1",
                    idempotency_key=f"migration:{job_id}",
                    created_at=func.now(),
                    updated_at=func.now(),
                )
            )
            connection.execute(
                attempts.insert().values(
                    attempt_id=attempt_id,
                    work_item_id=work_item_id,
                    job_id=job_id,
                    ordinal=1,
                    state="succeeded",
                    started_at=func.now(),
                    adapter_started_at=func.now(),
                    created_at=func.now(),
                )
            )
            connection.execute(
                budget_ledger.insert().values(
                    entry_id=reservation_id,
                    campaign_id=campaign_id,
                    work_item_id=work_item_id,
                    job_id=job_id,
                    attempt_id=None,
                    lease_generation=1,
                    reservation_entry_id=None,
                    kind="reserved",
                    amount=1,
                    unit="attempt",
                    reason="estimate reserved",
                    recorded_at=func.now(),
                )
            )
            connection.execute(
                budget_ledger.insert().values(
                    entry_id=settlement_id,
                    campaign_id=campaign_id,
                    work_item_id=work_item_id,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    lease_generation=1,
                    reservation_entry_id=reservation_id,
                    kind="consumed",
                    amount=2,
                    unit="attempt",
                    reason="actual cost incurred",
                    recorded_at=func.now(),
                )
            )

        command.downgrade(alembic_config, "61d3f47b809a")
        ledger_columns = {column["name"] for column in inspect(engine).get_columns("budget_ledger")}
        with engine.begin() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT entry_id,kind,amount,unit,reason FROM budget_ledger "
                        "WHERE entry_id IN (:reservation_id,:settlement_id) ORDER BY amount"
                    ),
                    {"reservation_id": reservation_id, "settlement_id": settlement_id},
                )
                .mappings()
                .all()
            )

        assert [(row["kind"], row["amount"], row["unit"], row["reason"]) for row in rows] == [
            ("reserved", 1, "attempt", "estimate reserved"),
            ("consumed", 2, "attempt", "actual cost incurred"),
        ]
        assert {
            "job_id",
            "attempt_id",
            "lease_generation",
            "reservation_entry_id",
        }.isdisjoint(ledger_columns)
    finally:
        _clear_all(engine)
        command.upgrade(alembic_config, "head")


def test_budget_reservation_downgrade_refuses_to_rewrite_actual_adjustments(
    engine: Engine, alembic_config: Config
) -> None:
    _clear_all(engine)
    campaign_id = uuid.uuid4()
    work_item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    entry_id = uuid.uuid4()
    try:
        with engine.begin() as connection:
            connection.execute(
                campaigns.insert().values(
                    campaign_id=campaign_id,
                    name="adjustment downgrade refusal",
                    environment_id="her",
                    adapter_version="1",
                    data_origin="synthetic",
                    execution_mode="replay",
                    state="active",
                    declaration={},
                    declaration_hash="f" * 64,
                    hard_budget=10,
                    per_attempt_estimate=1,
                    budget_unit="attempt",
                    max_attempts=3,
                    stopping_rule="hard_budget_exhausted",
                    event_stream_contract_version=1,
                    event_stream_last_position=0,
                    created_at=func.now(),
                    updated_at=func.now(),
                )
            )
            connection.execute(
                work_items.insert().values(
                    work_item_id=work_item_id,
                    campaign_id=campaign_id,
                    candidate_id="candidate:adjustment-downgrade",
                    candidate={"kind": "migration-test"},
                    state="accepted",
                    created_at=func.now(),
                    updated_at=func.now(),
                )
            )
            connection.execute(
                jobs.insert().values(
                    job_id=job_id,
                    work_item_id=work_item_id,
                    state="succeeded",
                    available_at=func.now(),
                    lease_generation=1,
                    attempt_count=1,
                    max_attempts=3,
                    command_version="1",
                    idempotency_key=f"adjustment-migration:{job_id}",
                    created_at=func.now(),
                    updated_at=func.now(),
                )
            )
            connection.execute(
                attempts.insert().values(
                    attempt_id=attempt_id,
                    work_item_id=work_item_id,
                    job_id=job_id,
                    ordinal=1,
                    state="lease_lost",
                    started_at=func.now(),
                    adapter_started_at=func.now(),
                    created_at=func.now(),
                )
            )
            connection.execute(
                budget_ledger.insert().values(
                    entry_id=reservation_id,
                    campaign_id=campaign_id,
                    work_item_id=work_item_id,
                    job_id=job_id,
                    lease_generation=1,
                    kind="reserved",
                    amount=1,
                    unit="attempt",
                    reason="estimate reserved",
                    recorded_at=func.now(),
                )
            )
            connection.execute(
                budget_ledger.insert().values(
                    entry_id=entry_id,
                    campaign_id=campaign_id,
                    work_item_id=work_item_id,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    lease_generation=1,
                    reservation_entry_id=reservation_id,
                    kind="adjusted_up",
                    amount=1,
                    unit="attempt",
                    reason="late actual adjustment retained",
                    recorded_at=func.now(),
                )
            )

        with pytest.raises(Exception, match="actual-cost adjustment ledger rows exist"):
            command.downgrade(alembic_config, "61d3f47b809a")

        with engine.begin() as connection:
            retained = connection.execute(
                select(budget_ledger.c.kind, budget_ledger.c.amount).where(
                    budget_ledger.c.entry_id == entry_id
                )
            ).one()
        assert retained.kind == "adjusted_up"
        assert retained.amount == 1
    finally:
        _clear_all(engine)
        command.upgrade(alembic_config, "head")

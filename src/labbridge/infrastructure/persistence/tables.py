"""The PostgreSQL schema.

`docs/SPEC.md` §4.1 makes PostgreSQL authoritative for campaigns, work items, events, jobs,
attempts and outcomes, budget, idempotency keys, object metadata, and correction relations.

SQLAlchemy Core rather than the ORM: this schema is read and written by a worker that cares about
exact SQL, atomic claims, and partial indexes. An identity map would add a layer between the code
and the guarantees it is trying to make.

**The constraints are the point.** A rule enforced only in Python holds until the next writer — a
migration, a repair script, an admin session. Three are therefore also enforced here:

* the admissible origin/mode pairs (ADR-010, invariant 1);
* at most one accepted outcome per work item, by partial unique index (PO-02);
* event sequences unique and per aggregate (§5.1).

A naming convention is declared so Alembic emits stable, comparable constraint names instead of
database-generated ones that differ between environments.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)

#: Mirrors `labbridge.domain.identity.ADMISSIBLE_PAIRS`. Duplicated deliberately: the database must
#: refuse an inadmissible pair even when the writer is not this application. `test_constraints.py`
#: enumerates the whole origin/mode product against the database, so the copies cannot drift apart.
_ADMISSIBLE_PAIR_SQL = (
    "(data_origin, execution_mode) IN "
    "(('observed','replay'),('synthetic','replay'),"
    "('synthetic','simulation'),('observed','live'))"
)

_ID = String(128)
_HASH = String(64)

#: Mirrors `domain.results.ObservationStatus` and `AttemptStatus`. Enumerated in SQL because the
#: PO-02 partial index keys on the literal `status = 'succeeded'`: without a value-set constraint a
#: writer inserting `'Succeeded'` or `'succeeded '` records a second success the index never sees.
_KNOWN_OBSERVATION_STATUS = (
    "status IN ('received','accepted','corrupted','invalidated','superseded')"
)
_KNOWN_ATTEMPT_STATUS = (
    "status IN ('succeeded','timed_out','failed_retryable','failed_terminal',"
    "'corrupted','cancelled','lease_lost','duplicate_suppressed')"
)


def _timestamps(*names: str) -> list[Column[datetime]]:
    """Timestamps are always timezone-aware: a naive column cannot be ordered across hosts."""
    return [Column(name, DateTime(timezone=True), nullable=False) for name in names]


campaigns = Table(
    "campaigns",
    metadata,
    Column("campaign_id", UUID(as_uuid=True), primary_key=True),
    Column("name", Text, nullable=False),
    Column("environment_id", String(64), nullable=False),
    Column("adapter_version", String(64), nullable=False),
    Column("data_origin", String(16), nullable=False),
    Column("execution_mode", String(16), nullable=False),
    Column("state", String(32), nullable=False),
    Column("declaration", JSONB, nullable=False),
    Column("declaration_hash", _HASH, nullable=False),
    *_timestamps("created_at", "updated_at"),
    CheckConstraint(_ADMISSIBLE_PAIR_SQL, name="admissible_origin_mode"),
    # Redundant on its own — campaign_id is already unique — but it is the target a child row's
    # composite foreign key needs. That is what forces every observation and outcome to carry the
    # *same* origin and mode as its campaign, rather than merely a separately-admissible pair.
    UniqueConstraint("campaign_id", "data_origin", "execution_mode", name="uq_campaigns_identity"),
)

work_items = Table(
    "work_items",
    metadata,
    Column("work_item_id", UUID(as_uuid=True), primary_key=True),
    Column(
        "campaign_id",
        UUID(as_uuid=True),
        ForeignKey("campaigns.campaign_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("candidate_id", _ID, nullable=False),
    Column("candidate", JSONB, nullable=False),
    Column("state", String(32), nullable=False),
    Column("quarantine_reason", Text, nullable=True),
    *_timestamps("created_at", "updated_at"),
    # One work item per candidate per campaign: re-proposing the same location is the same item, and
    # a repeat is a new attempt on it rather than a second item.
    UniqueConstraint("campaign_id", "candidate_id", name="uq_work_items_campaign_candidate"),
    CheckConstraint(
        "state <> 'quarantined' OR quarantine_reason IS NOT NULL",
        name="quarantine_states_its_reason",
    ),
)

jobs = Table(
    "jobs",
    metadata,
    Column("job_id", UUID(as_uuid=True), primary_key=True),
    Column(
        "work_item_id",
        UUID(as_uuid=True),
        ForeignKey("work_items.work_item_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("state", String(32), nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False),
    # A lease is (owner, token, expiry). The token is what stops a worker that lost its lease from
    # completing the job anyway after a pause: it must present the token the claim wrote.
    Column("lease_owner", String(128), nullable=True),
    Column("lease_token", UUID(as_uuid=True), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("heartbeat_at", DateTime(timezone=True), nullable=True),
    Column("attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("max_attempts", Integer, nullable=False),
    Column("command_version", String(32), nullable=False),
    Column("idempotency_key", String(255), nullable=False),
    Column("last_failure", JSONB, nullable=True),
    *_timestamps("created_at", "updated_at"),
    UniqueConstraint("idempotency_key", name="uq_jobs_idempotency_key"),
    CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
    CheckConstraint(
        "(lease_owner IS NULL) = (lease_expires_at IS NULL)",
        name="lease_owner_and_expiry_together",
    ),
)

# The claim query orders by availability among available jobs only. A partial index keeps it off the
# rows a claim can never select.
Index(
    "ix_jobs_claimable",
    jobs.c.available_at,
    postgresql_where=text("state = 'available'"),
)

attempts = Table(
    "attempts",
    metadata,
    Column("attempt_id", UUID(as_uuid=True), primary_key=True),
    Column(
        "work_item_id",
        UUID(as_uuid=True),
        ForeignKey("work_items.work_item_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("job_id", UUID(as_uuid=True), ForeignKey("jobs.job_id", ondelete="RESTRICT")),
    # 1, 2, 3 … within a work item. A retry creates a new attempt; it never rewrites this row.
    Column("ordinal", Integer, nullable=False),
    Column("state", String(32), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=True),
    *_timestamps("created_at"),
    UniqueConstraint("work_item_id", "ordinal", name="uq_attempts_work_item_ordinal"),
    CheckConstraint("ordinal >= 1", name="ordinal_starts_at_one"),
)

observations = Table(
    "observations",
    metadata,
    # Content-derived: the same bytes under the same schema and provenance root yield the same
    # `observation_id`. That identity is deliberately *not* the primary key on its own. Two
    # campaigns replaying one fixture location produce identical content, and a bare PK would
    # either reject the second receipt — losing the bytes invariant 2 requires retaining — or
    # silently attribute them to the first campaign's attempt. One row per (content, attempt)
    # keeps both the content identity and the honest attribution.
    Column("observation_id", _ID, primary_key=True),
    Column("campaign_id", UUID(as_uuid=True), nullable=False),
    Column(
        "work_item_id",
        UUID(as_uuid=True),
        ForeignKey("work_items.work_item_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "attempt_id",
        UUID(as_uuid=True),
        ForeignKey("attempts.attempt_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column("media_type", String(128), nullable=False),
    Column("object_uri", Text, nullable=False),
    Column("byte_size", BigInteger, nullable=False),
    Column("sha256", _HASH, nullable=False),
    Column("schema_version", String(32), nullable=False),
    Column("signal_kind", String(32), nullable=False),
    Column("quantities", JSONB, nullable=False),
    Column("status", String(32), nullable=False),
    Column("status_reason", Text, nullable=True),
    Column("data_origin", String(16), nullable=False),
    Column("execution_mode", String(16), nullable=False),
    Column("provenance", JSONB, nullable=False),
    *_timestamps("received_at"),
    CheckConstraint(_ADMISSIBLE_PAIR_SQL, name="admissible_origin_mode"),
    CheckConstraint("byte_size >= 0", name="byte_size_non_negative"),
    # ADR-005 and PO-05: a corrupted or invalidated observation keeps its bytes and states why.
    CheckConstraint(
        "status NOT IN ('corrupted','invalidated') OR status_reason IS NOT NULL",
        name="rejected_status_states_its_reason",
    ),
    CheckConstraint(_KNOWN_OBSERVATION_STATUS, name="known_status"),
    # The pair must equal the campaign's, not merely be admissible on its own. Without this a
    # `synthetic + replay` observation inserts cleanly into an `observed + replay` campaign: both
    # rows pass the CHECK individually, and the conflation ADR-010 exists to prevent happens
    # between rows rather than within one.
    ForeignKeyConstraint(
        ["campaign_id", "data_origin", "execution_mode"],
        ["campaigns.campaign_id", "campaigns.data_origin", "campaigns.execution_mode"],
        name="fk_observations_campaign_identity",
        ondelete="RESTRICT",
    ),
)

attempt_outcomes = Table(
    "attempt_outcomes",
    metadata,
    # One outcome per attempt, enforced by making the attempt the primary key.
    Column(
        "attempt_id",
        UUID(as_uuid=True),
        ForeignKey("attempts.attempt_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column(
        "work_item_id",
        UUID(as_uuid=True),
        ForeignKey("work_items.work_item_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("campaign_id", UUID(as_uuid=True), nullable=False),
    Column("status", String(32), nullable=False),
    Column("observation_id", _ID, nullable=True),
    Column("failure", JSONB, nullable=True),
    Column("cost", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    # Provenance is mandatory on the domain model and normative in docs/SPEC.md §3.6. Without these
    # columns a writer drops origin and mode at the persistence boundary, and a report over
    # outcomes has to re-derive them by joining `campaigns` — the "reconstruct downstream" that
    # invariant 1 forbids. An outcome with no observation (timed_out, lease_lost) would otherwise
    # leave `code_version` and the lineage root recoverable from no row at all.
    Column("data_origin", String(16), nullable=False),
    Column("execution_mode", String(16), nullable=False),
    Column("provenance", JSONB, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=True),
    *_timestamps("finished_at"),
    # Only an outcome that received bytes may reference an observation, and the observation it
    # references must be the one *this attempt* received — hence the composite key.
    ForeignKeyConstraint(
        ["observation_id", "attempt_id"],
        ["observations.observation_id", "observations.attempt_id"],
        name="fk_attempt_outcomes_observation",
        ondelete="RESTRICT",
    ),
    CheckConstraint(
        "observation_id IS NULL OR status IN ('succeeded','corrupted')",
        name="observation_only_when_bytes_arrived",
    ),
    CheckConstraint(
        "status NOT IN ('failed_retryable','failed_terminal') OR failure IS NOT NULL",
        name="failed_status_carries_a_failure",
    ),
    CheckConstraint(_KNOWN_ATTEMPT_STATUS, name="known_status"),
    CheckConstraint(_ADMISSIBLE_PAIR_SQL, name="admissible_origin_mode"),
    ForeignKeyConstraint(
        ["campaign_id", "data_origin", "execution_mode"],
        ["campaigns.campaign_id", "campaigns.data_origin", "campaigns.execution_mode"],
        name="fk_attempt_outcomes_campaign_identity",
        ondelete="RESTRICT",
    ),
)

# PO-02: no unintended duplicate acceptance. At most one succeeded outcome per work item, whatever
# the delivery count. A duplicate delivery hits this index and is suppressed rather than accepted.
Index(
    "uq_attempt_outcomes_one_success_per_work_item",
    attempt_outcomes.c.work_item_id,
    unique=True,
    postgresql_where=text("status = 'succeeded'"),
)

derived_metrics = Table(
    "derived_metrics",
    metadata,
    Column("metric_id", _ID, primary_key=True),
    Column("observation_id", _ID, nullable=False),
    # Which *receipt* the metric was derived from. Two campaigns can receive identical content, and
    # a metric computed from one is not evidence about the other: they carry different attempts,
    # different costs, and can be invalidated independently.
    Column(
        "attempt_id",
        UUID(as_uuid=True),
        ForeignKey("attempts.attempt_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("name", String(128), nullable=False),
    Column("value_numeric", Numeric, nullable=True),
    Column("value", JSONB, nullable=False),
    Column("unit", String(64), nullable=False),
    Column("uncertainty", JSONB, nullable=True),
    Column("analysis_name", String(128), nullable=False),
    Column("analysis_version", String(32), nullable=False),
    Column("parameter_hash", _HASH, nullable=False),
    Column("quality_status", String(16), nullable=False),
    Column("quality_reason", Text, nullable=True),
    Column("provenance", JSONB, nullable=False),
    *_timestamps("created_at"),
    # §3.6: a source-provided fit and a LabBridge recomputation are distinct rows, never merged.
    # `attempt_id` is part of the key for the same reason it is part of the foreign key: the same
    # analysis over two receipts of identical content is two results, not a duplicate.
    UniqueConstraint(
        "observation_id",
        "attempt_id",
        "name",
        "analysis_name",
        "analysis_version",
        "parameter_hash",
        name="uq_derived_metrics_analysis",
    ),
    ForeignKeyConstraint(
        ["observation_id", "attempt_id"],
        ["observations.observation_id", "observations.attempt_id"],
        name="fk_derived_metrics_observation",
        ondelete="RESTRICT",
    ),
    CheckConstraint(
        "quality_status = 'accepted' OR quality_reason IS NOT NULL",
        name="non_accepted_metric_states_its_reason",
    ),
)

events = Table(
    "events",
    metadata,
    Column("event_id", UUID(as_uuid=True), primary_key=True),
    Column(
        "campaign_id",
        UUID(as_uuid=True),
        ForeignKey("campaigns.campaign_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("aggregate_id", UUID(as_uuid=True), nullable=False),
    Column("aggregate_type", String(64), nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("event_type", String(128), nullable=False),
    Column("schema_version", Integer, nullable=False),
    *_timestamps("occurred_at", "recorded_at"),
    Column("correlation_id", UUID(as_uuid=True), nullable=False),
    Column("causation_id", UUID(as_uuid=True), nullable=True),
    Column("idempotency_key", String(255), nullable=True),
    Column("payload", JSONB, nullable=False),
    # §5.1: sequence unique and monotonic per aggregate. This is what makes an append with an
    # expected version safe under concurrency — the loser of a race gets a constraint violation.
    UniqueConstraint("aggregate_id", "sequence", name="uq_events_aggregate_sequence"),
    CheckConstraint("sequence >= 1", name="sequence_starts_at_one"),
    CheckConstraint("schema_version >= 1", name="schema_version_starts_at_one"),
)

# Replay reads a campaign's events ordered by aggregate then sequence, never by timestamp.
Index("ix_events_replay", events.c.campaign_id, events.c.aggregate_id, events.c.sequence)

storage_objects = Table(
    "storage_objects",
    metadata,
    Column("object_uri", Text, primary_key=True),
    Column("bucket", String(128), nullable=False),
    Column("object_key", Text, nullable=False),
    Column("byte_size", BigInteger, nullable=True),
    Column("sha256", _HASH, nullable=True),
    # §4.2: a record must not declare an artifact committed before the object exists and its
    # checksum has been verified, so the checksum is required exactly in that state.
    Column("state", String(16), nullable=False),
    *_timestamps("created_at"),
    Column("committed_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("state IN ('pending','committed','orphaned')", name="known_object_state"),
    CheckConstraint(
        "state <> 'committed' OR (sha256 IS NOT NULL AND byte_size IS NOT NULL "
        "AND committed_at IS NOT NULL)",
        name="committed_object_is_verified",
    ),
)

idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    Column("idempotency_key", String(255), primary_key=True),
    Column("scope", String(64), nullable=False),
    Column("request_hash", _HASH, nullable=False),
    Column("response", JSONB, nullable=True),
    *_timestamps("created_at"),
)

budget_ledger = Table(
    "budget_ledger",
    metadata,
    Column("entry_id", UUID(as_uuid=True), primary_key=True),
    Column(
        "campaign_id",
        UUID(as_uuid=True),
        ForeignKey("campaigns.campaign_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("work_item_id", UUID(as_uuid=True), nullable=True),
    # Append-only: a reservation and its release are two rows, never an update of one.
    Column("kind", String(16), nullable=False),
    Column("amount", Numeric, nullable=False),
    Column("unit", String(32), nullable=False),
    Column("reason", Text, nullable=False),
    *_timestamps("recorded_at"),
    CheckConstraint("kind IN ('reserved','consumed','released')", name="known_ledger_kind"),
)

record_relations = Table(
    "record_relations",
    metadata,
    Column("relation_id", UUID(as_uuid=True), primary_key=True),
    Column("subject_id", _ID, nullable=False),
    Column("predicate", String(32), nullable=False),
    Column("object_id", _ID, nullable=False),
    Column("reason", Text, nullable=False),
    *_timestamps("recorded_at"),
    UniqueConstraint("subject_id", "predicate", "object_id", name="uq_record_relations_triple"),
    CheckConstraint("subject_id <> object_id", name="no_self_relation"),
    CheckConstraint(
        "predicate IN ('derived_from','supersedes','invalidates','replaces')",
        name="known_predicate",
    ),
)

__all__ = [
    "attempt_outcomes",
    "attempts",
    "budget_ledger",
    "campaigns",
    "derived_metrics",
    "events",
    "idempotency_keys",
    "jobs",
    "metadata",
    "observations",
    "record_relations",
    "storage_objects",
    "work_items",
]

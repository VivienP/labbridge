"""The PostgreSQL schema.

`docs/SPEC.md` §4.1 makes PostgreSQL authoritative for campaigns, work items, events, jobs,
attempts and outcomes, budget, idempotency keys, object metadata, and correction relations.

SQLAlchemy Core rather than the ORM: this schema is read and written by a worker that cares about
exact SQL, atomic claims, and partial indexes. An identity map would add a layer between the code
and the guarantees it is trying to make.

**The constraints are the point.** A rule enforced only in Python holds until the next writer — a
migration, a repair script, an admin session. Four are therefore also enforced here:

* the admissible origin/mode pairs (ADR-010, invariant 1);
* at most one accepted outcome per work item, by partial unique index (PO-02);
* one campaign per scoped idempotency key, and one job per instruction key (ADR-015, PO-02);
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
    PrimaryKeyConstraint,
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

#: Mirrors `labbridge.domain.objects.OBJECT_CLASSIFICATIONS`, duplicated for the same reason as the
#: origin/mode pairs: the database must refuse an unknown verdict even when the writer is not this
#: application. `test_constraints.py` enumerates the domain tuple against the database, so the two
#: copies cannot drift apart.
_KNOWN_OBJECT_CLASSIFICATION = (
    "classification IS NULL OR classification IN "
    "('accepted_evidence','diagnostic_duplicate','diagnostic_orphan','quarantined','missing')"
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
    Column("event_stream_contract_version", Integer, nullable=False, server_default=text("0")),
    Column("event_stream_last_position", BigInteger, nullable=False, server_default=text("0")),
    *_timestamps("created_at", "updated_at"),
    CheckConstraint(_ADMISSIBLE_PAIR_SQL, name="admissible_origin_mode"),
    CheckConstraint(
        "event_stream_contract_version IN (0, 1)", name="known_event_stream_contract_version"
    ),
    CheckConstraint("event_stream_last_position >= 0", name="event_stream_position_non_negative"),
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
    #: The fencing token. Monotonic per job: every claim and every reclaim increments it, and it is
    #: never reset, so "which lease is newer" is answerable — which a random token cannot answer.
    #: It rides alongside `lease_token` rather than replacing it because the two do different jobs:
    #: the generation orders leases, the token proves the presenter is the holder rather than
    #: someone who guessed the next integer. Both are checked wherever ownership decides an effect.
    Column("lease_generation", BigInteger, nullable=False, server_default=text("0")),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("heartbeat_at", DateTime(timezone=True), nullable=True),
    Column("attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("max_attempts", Integer, nullable=False),
    Column("command_version", String(32), nullable=False),
    Column("idempotency_key", String(255), nullable=False),
    Column("last_failure", JSONB, nullable=True),
    Column("event_correlation_id", UUID(as_uuid=True), nullable=True),
    Column("last_event_id", UUID(as_uuid=True), nullable=True),
    *_timestamps("created_at", "updated_at"),
    UniqueConstraint("idempotency_key", name="uq_jobs_idempotency_key"),
    CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
    CheckConstraint("lease_generation >= 0", name="lease_generation_non_negative"),
    CheckConstraint(
        "(lease_owner IS NULL) = (lease_expires_at IS NULL)",
        name="lease_owner_and_expiry_together",
    ),
    # A held lease has all three parts. Without this a row could carry an owner and an expiry but no
    # token, and the fencing check would compare against NULL and silently never match.
    CheckConstraint(
        "(lease_owner IS NULL) = (lease_token IS NULL)",
        name="lease_owner_and_token_together",
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
    # A rejected receipt still has to say why it was rejected, exactly as a corrupted one does.
    CheckConstraint(
        "status <> 'received' OR status_reason IS NOT NULL",
        name="retained_receipt_states_its_reason",
    ),
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

# At most one *accepted* observation per work item, whatever the delivery or retry count. The
# outcome index below says at most one attempt succeeded; this one says the evidence agrees, so a
# writer that bypassed the worker cannot leave two accepted receipts pointing at one item. Receipts
# retained as `received` are deliberately outside the predicate: that is how a refused delivery
# keeps its bytes without competing for acceptance.
Index(
    "uq_observations_one_accepted_per_work_item",
    observations.c.work_item_id,
    unique=True,
    postgresql_where=text("status = 'accepted'"),
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
    # An outcome may reference an observation exactly when bytes arrived. `duplicate_suppressed` and
    # `lease_lost` are here because both can occur *after* an adapter returned and its bytes were
    # stored: the result is refused from accepted state, but the bytes were received and invariant 2
    # requires them retained. What they reference is a `received` observation, never an `accepted`
    # one — the partial index below is what keeps that distinction from being a matter of care.
    CheckConstraint(
        "observation_id IS NULL "
        "OR status IN ('succeeded','corrupted','duplicate_suppressed','lease_lost')",
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
    #: How the value was normalised, when the unit alone does not say. `A/cm^2` states the
    #: dimension; it does not say *which* cm² — geometric, meniscus contact, or ECSA — and the
    #: archive states none of them. Encoding it into the unit string would break every unit
    #: comparison, so it rides its own column and reaches the exported bundle.
    Column("normalisation_basis", String(32), nullable=True),
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
    Column("campaign_position", BigInteger, nullable=False),
    Column("event_type", String(128), nullable=False),
    Column("schema_version", Integer, nullable=False),
    *_timestamps("occurred_at", "recorded_at"),
    Column("correlation_id", UUID(as_uuid=True), nullable=False),
    Column("causation_id", UUID(as_uuid=True), nullable=True),
    Column("idempotency_key", String(255), nullable=True),
    Column("payload", JSONB, nullable=False),
    # §5.1: sequence is unique per aggregate. The expected-version check and campaign row lock
    # allocate it monotonically; this constraint prevents a bypassing writer from duplicating it.
    UniqueConstraint(
        "campaign_id",
        "aggregate_type",
        "aggregate_id",
        "sequence",
        name="uq_events_aggregate_sequence",
    ),
    UniqueConstraint("campaign_id", "campaign_position", name="uq_events_campaign_position"),
    CheckConstraint("sequence >= 1", name="sequence_starts_at_one"),
    CheckConstraint("campaign_position >= 1", name="campaign_position_starts_at_one"),
    CheckConstraint("schema_version >= 1", name="schema_version_starts_at_one"),
)

# Complete stream loading follows the campaign-wide position, never timestamps or aggregate IDs.
Index("ix_events_replay", events.c.campaign_id, events.c.campaign_position)

storage_objects = Table(
    "storage_objects",
    metadata,
    Column("object_uri", Text, primary_key=True),
    Column("bucket", String(128), nullable=False),
    Column("object_key", Text, nullable=False),
    Column("byte_size", BigInteger, nullable=True),
    Column("sha256", _HASH, nullable=True),
    Column("media_type", String(128), nullable=True),
    # Which execution put these bytes here. Without it a reconciler can say an object is
    # unreferenced but not which attempt to attribute it to, and an orphan becomes anonymous the
    # moment the process that wrote it is gone — precisely when reconciliation has to run.
    Column(
        "attempt_id",
        UUID(as_uuid=True),
        ForeignKey("attempts.attempt_id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column(
        "work_item_id",
        UUID(as_uuid=True),
        ForeignKey("work_items.work_item_id", ondelete="RESTRICT"),
        nullable=True,
    ),
    # §4.2: a record must not declare an artifact committed before the object exists and its
    # checksum has been verified, so the checksum is required exactly in that state.
    Column("state", String(16), nullable=False),
    #: What reconciliation concluded about this object, and why. Separate from `state` on purpose:
    #: `state` is the write lifecycle the worker drives, `classification` is the verdict a later
    #: pass reaches by comparing the row against the store. Keeping them apart means a
    #: reconciliation verdict never silently rewrites the history of how the object was written.
    Column("classification", String(32), nullable=True),
    Column("classification_reason", Text, nullable=True),
    Column("reconciled_at", DateTime(timezone=True), nullable=True),
    *_timestamps("created_at"),
    Column("committed_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("state IN ('pending','committed','orphaned')", name="known_object_state"),
    CheckConstraint(
        "state <> 'committed' OR (sha256 IS NOT NULL AND byte_size IS NOT NULL "
        "AND committed_at IS NOT NULL)",
        name="committed_object_is_verified",
    ),
    CheckConstraint(_KNOWN_OBJECT_CLASSIFICATION, name="known_classification"),
    # A verdict and the evidence for it arrive together, or the classification is unauditable.
    CheckConstraint(
        "(classification IS NULL) = (reconciled_at IS NULL)",
        name="classification_records_when_it_was_reached",
    ),
    CheckConstraint(
        "classification IS NULL OR classification_reason IS NOT NULL",
        name="classification_states_its_reason",
    ),
)

source_artifacts = Table(
    "source_artifacts",
    metadata,
    Column("source_artifact_id", _ID, primary_key=True),
    Column("filename", Text, nullable=False),
    Column("media_type", String(128), nullable=False),
    Column("byte_size", BigInteger, nullable=False),
    Column("sha256", _HASH, nullable=False),
    Column("data_origin", String(16), nullable=False),
    Column("execution_mode", String(16), nullable=False),
    Column("state", String(16), nullable=False),
    Column(
        "object_uri",
        Text,
        ForeignKey("storage_objects.object_uri", ondelete="RESTRICT"),
        nullable=False,
    ),
    *_timestamps("created_at"),
    Column("committed_at", DateTime(timezone=True), nullable=True),
    Column("quarantine_reason", Text, nullable=True),
    CheckConstraint("byte_size >= 0", name="source_byte_size_non_negative"),
    CheckConstraint(_ADMISSIBLE_PAIR_SQL, name="source_admissible_origin_mode"),
    CheckConstraint("state IN ('pending','committed','quarantined')", name="known_source_state"),
    CheckConstraint(
        "(state = 'committed') = (committed_at IS NOT NULL)",
        name="committed_source_has_timestamp",
    ),
    CheckConstraint(
        "(state = 'quarantined') = (quarantine_reason IS NOT NULL)",
        name="quarantined_source_has_reason",
    ),
)

normalised_observations = Table(
    "normalised_observations",
    metadata,
    Column("observation_id", _ID, primary_key=True),
    Column("technique", String(64), nullable=False),
    *_timestamps("created_at"),
    CheckConstraint(
        "technique IN ('cyclic_voltammetry','galvanostatic_electrolysis')",
        name="known_normalised_observation_technique",
    ),
)

import_profiles = Table(
    "import_profiles",
    metadata,
    Column("profile_id", _ID, primary_key=True),
    Column("schema_version", String(16), nullable=False),
    Column("technique", String(64), nullable=False),
    Column("body", JSONB, nullable=False),
    *_timestamps("created_at"),
    CheckConstraint("schema_version = '1'", name="known_import_profile_schema"),
    CheckConstraint("technique = 'cyclic_voltammetry'", name="known_import_profile_technique"),
)

normalised_cv_observations = Table(
    "normalised_cv_observations",
    metadata,
    Column(
        "observation_id",
        _ID,
        ForeignKey("normalised_observations.observation_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column(
        "source_artifact_id",
        _ID,
        ForeignKey("source_artifacts.source_artifact_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "profile_id",
        _ID,
        ForeignKey("import_profiles.profile_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("schema_version", String(16), nullable=False),
    Column("parser_version", String(64), nullable=False),
    Column("normalisation_version", String(64), nullable=False),
    Column("data_origin", String(16), nullable=False),
    Column("execution_mode", String(16), nullable=False),
    Column("environment_id", String(128), nullable=False),
    Column("row_count", BigInteger, nullable=False),
    Column(
        "object_uri",
        Text,
        ForeignKey("storage_objects.object_uri", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("byte_size", BigInteger, nullable=False),
    Column("sha256", _HASH, nullable=False),
    *_timestamps("created_at"),
    CheckConstraint("schema_version = '1'", name="known_normalised_cv_schema"),
    CheckConstraint("row_count >= 1", name="normalised_cv_has_rows"),
    CheckConstraint("byte_size >= 0", name="normalised_cv_byte_size_non_negative"),
    CheckConstraint(_ADMISSIBLE_PAIR_SQL, name="normalised_cv_admissible_origin_mode"),
)

cv_parser_records = Table(
    "cv_parser_records",
    metadata,
    Column("parser_record_id", _ID, primary_key=True),
    Column(
        "source_artifact_id",
        _ID,
        ForeignKey("source_artifacts.source_artifact_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "profile_id",
        _ID,
        ForeignKey("import_profiles.profile_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "observation_id",
        _ID,
        ForeignKey("normalised_cv_observations.observation_id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    ),
    Column("source_format", String(32), nullable=False),
    Column("parser_version", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("body", JSONB, nullable=False),
    *_timestamps("created_at"),
    CheckConstraint("source_format IN ('generic_csv','gamry_dta')", name="known_source_format"),
    CheckConstraint("status IN ('accepted','rejected')", name="known_parser_status"),
    CheckConstraint(
        "(status = 'accepted' AND observation_id IS NOT NULL) OR "
        "(status = 'rejected' AND observation_id IS NULL)",
        name="parser_status_matches_observation",
    ),
)

cv_transformation_records = Table(
    "cv_transformation_records",
    metadata,
    Column("transformation_id", _ID, primary_key=True),
    Column(
        "observation_id",
        _ID,
        ForeignKey("normalised_cv_observations.observation_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("ordinal", Integer, nullable=False),
    Column("record", JSONB, nullable=False),
    UniqueConstraint("observation_id", "ordinal", name="uq_cv_transform_observation_ordinal"),
    CheckConstraint("ordinal >= 1", name="cv_transform_ordinal_starts_at_one"),
)

cv_structural_findings = Table(
    "cv_structural_findings",
    metadata,
    Column("finding_id", _ID, primary_key=True),
    Column(
        "observation_id",
        _ID,
        ForeignKey("normalised_cv_observations.observation_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("finding", JSONB, nullable=False),
)

electrolysis_import_profiles = Table(
    "electrolysis_import_profiles",
    metadata,
    Column("profile_id", _ID, primary_key=True),
    Column("schema_version", String(16), nullable=False),
    Column("technique", String(64), nullable=False),
    Column("body", JSONB, nullable=False),
    *_timestamps("created_at"),
    CheckConstraint("schema_version = '1'", name="known_electrolysis_profile_schema"),
    CheckConstraint(
        "technique = 'galvanostatic_electrolysis'",
        name="known_electrolysis_profile_technique",
    ),
)

normalised_electrolysis_observations = Table(
    "normalised_electrolysis_observations",
    metadata,
    Column(
        "observation_id",
        _ID,
        ForeignKey("normalised_observations.observation_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column(
        "source_artifact_id",
        _ID,
        ForeignKey("source_artifacts.source_artifact_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "profile_id",
        _ID,
        ForeignKey("electrolysis_import_profiles.profile_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("schema_version", String(16), nullable=False),
    Column("parser_version", String(64), nullable=False),
    Column("normalisation_version", String(64), nullable=False),
    Column("data_origin", String(16), nullable=False),
    Column("execution_mode", String(16), nullable=False),
    Column("environment_id", String(128), nullable=False),
    Column("row_count", BigInteger, nullable=False),
    Column(
        "object_uri",
        Text,
        ForeignKey("storage_objects.object_uri", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("byte_size", BigInteger, nullable=False),
    Column("sha256", _HASH, nullable=False),
    *_timestamps("created_at"),
    CheckConstraint("schema_version = '1'", name="known_electrolysis_observation_schema"),
    CheckConstraint("row_count >= 1", name="electrolysis_observation_has_rows"),
    CheckConstraint("byte_size >= 0", name="electrolysis_observation_byte_size_non_negative"),
    CheckConstraint(_ADMISSIBLE_PAIR_SQL, name="electrolysis_observation_admissible_origin_mode"),
)

electrolysis_transformation_records = Table(
    "electrolysis_transformation_records",
    metadata,
    Column("transformation_id", _ID, primary_key=True),
    Column(
        "observation_id",
        _ID,
        ForeignKey("normalised_electrolysis_observations.observation_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("ordinal", Integer, nullable=False),
    Column("record", JSONB, nullable=False),
    UniqueConstraint(
        "observation_id", "ordinal", name="uq_electrolysis_transform_observation_ordinal"
    ),
    CheckConstraint("ordinal >= 1", name="electrolysis_transform_ordinal_starts_at_one"),
)

electrolysis_structural_findings = Table(
    "electrolysis_structural_findings",
    metadata,
    Column("finding_id", _ID, primary_key=True),
    Column(
        "observation_id",
        _ID,
        ForeignKey("normalised_electrolysis_observations.observation_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("finding", JSONB, nullable=False),
)

electrolysis_auxiliary_results = Table(
    "electrolysis_auxiliary_results",
    metadata,
    Column("result_id", _ID, primary_key=True),
    Column(
        "observation_id",
        _ID,
        ForeignKey("normalised_electrolysis_observations.observation_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "source_artifact_id",
        _ID,
        ForeignKey("source_artifacts.source_artifact_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("method_name", String(128), nullable=False),
    Column("method_version", String(64), nullable=False),
    Column("body", JSONB, nullable=False),
    *_timestamps("created_at"),
)

experiments = Table(
    "experiments",
    metadata,
    Column("experiment_id", _ID, primary_key=True),
    Column(
        "observation_id",
        _ID,
        ForeignKey("normalised_observations.observation_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    Column("schema_version", String(16), nullable=False),
    Column("current_version", Integer, nullable=False),
    Column("technique", String(64), nullable=False),
    Column("data_origin", String(16), nullable=False),
    Column("execution_mode", String(16), nullable=False),
    Column("environment_id", String(128), nullable=False),
    *_timestamps("created_at", "updated_at"),
    CheckConstraint("schema_version = '1'", name="known_experiment_schema"),
    CheckConstraint("current_version >= 1", name="experiment_version_starts_at_one"),
    CheckConstraint(
        "technique IN ('cyclic_voltammetry','galvanostatic_electrolysis')",
        name="known_experiment_technique",
    ),
    CheckConstraint(_ADMISSIBLE_PAIR_SQL, name="experiment_admissible_origin_mode"),
)

experiment_versions = Table(
    "experiment_versions",
    metadata,
    Column("experiment_id", _ID, nullable=False),
    Column("version", Integer, nullable=False),
    Column("supersedes_version", Integer, nullable=True),
    Column("body", JSONB, nullable=False),
    *_timestamps("created_at"),
    ForeignKeyConstraint(
        ["experiment_id"],
        ["experiments.experiment_id"],
        name="fk_experiment_versions_experiment_id_experiments",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["experiment_id", "supersedes_version"],
        ["experiment_versions.experiment_id", "experiment_versions.version"],
        name="fk_experiment_versions_superseded_version",
        ondelete="RESTRICT",
    ),
    PrimaryKeyConstraint("experiment_id", "version", name="pk_experiment_versions"),
    CheckConstraint("version >= 1", name="experiment_snapshot_version_starts_at_one"),
    CheckConstraint(
        "(version = 1 AND supersedes_version IS NULL) OR "
        "(version > 1 AND supersedes_version = version - 1)",
        name="experiment_snapshot_supersedes_predecessor",
    ),
)

metadata_assertions = Table(
    "metadata_assertions",
    metadata,
    Column("assertion_id", _ID, primary_key=True),
    Column("experiment_id", _ID, nullable=False),
    Column("created_version", Integer, nullable=False),
    Column("schema_version", String(16), nullable=False),
    Column("field_name", String(128), nullable=False),
    Column("origin", String(32), nullable=False),
    Column("transformation", String(32), nullable=False),
    Column("requirement_class", String(32), nullable=False),
    Column("value_state", String(32), nullable=False),
    Column("supplements_assertion_id", _ID, nullable=True),
    Column("supersedes_assertion_id", _ID, nullable=True),
    Column("body", JSONB, nullable=False),
    ForeignKeyConstraint(
        ["experiment_id", "created_version"],
        ["experiment_versions.experiment_id", "experiment_versions.version"],
        name="fk_metadata_assertions_experiment_version",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["supplements_assertion_id"],
        ["metadata_assertions.assertion_id"],
        name="fk_metadata_assertions_supplements_assertion",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["supersedes_assertion_id"],
        ["metadata_assertions.assertion_id"],
        name="fk_metadata_assertions_supersedes_assertion",
        ondelete="RESTRICT",
    ),
    CheckConstraint("schema_version = '1'", name="known_metadata_assertion_schema"),
    CheckConstraint("created_version >= 1", name="assertion_version_starts_at_one"),
    CheckConstraint(
        "origin IN ('source_file','user_supplied','inferred')", name="known_assertion_origin"
    ),
    CheckConstraint(
        "transformation IN ('none','parsed','unit_converted','derived')",
        name="known_assertion_transformation",
    ),
    CheckConstraint(
        "requirement_class IN ('required','conditional','recommended','optional')",
        name="known_assertion_requirement_class",
    ),
    CheckConstraint(
        "value_state IN ('known','unknown','unavailable','not_applicable')",
        name="known_assertion_value_state",
    ),
    Index(
        "ix_metadata_assertions_query_dimensions",
        "experiment_id",
        "field_name",
        "origin",
        "transformation",
        "requirement_class",
        "value_state",
    ),
)

validation_runs = Table(
    "validation_runs",
    metadata,
    Column("validation_id", _ID, primary_key=True),
    Column("experiment_id", _ID, nullable=False),
    Column("experiment_version", Integer, nullable=False),
    Column("schema_version", String(16), nullable=False),
    Column("validation_version", String(64), nullable=False),
    Column("release_status", String(16), nullable=False),
    Column("body", JSONB, nullable=False),
    *_timestamps("created_at"),
    ForeignKeyConstraint(
        ["experiment_id", "experiment_version"],
        ["experiment_versions.experiment_id", "experiment_versions.version"],
        name="fk_validation_runs_experiment_version",
        ondelete="RESTRICT",
    ),
    CheckConstraint("schema_version = '1'", name="known_validation_schema"),
    CheckConstraint("release_status IN ('blocked','eligible')", name="known_release_decision"),
)

validation_findings = Table(
    "validation_findings",
    metadata,
    Column("finding_id", _ID, primary_key=True),
    Column(
        "validation_id",
        _ID,
        ForeignKey("validation_runs.validation_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("experiment_id", _ID, nullable=False),
    Column("field_name", String(128), nullable=False),
    Column("severity", String(16), nullable=False),
    Column("requirement_class", String(32), nullable=False),
    Column("body", JSONB, nullable=False),
    CheckConstraint("severity IN ('blocking','warning','unknown')", name="known_finding_severity"),
    CheckConstraint(
        "requirement_class IN ('required','conditional','recommended','optional')",
        name="known_finding_requirement_class",
    ),
    Index(
        "ix_validation_findings_query_dimensions",
        "experiment_id",
        "severity",
        "requirement_class",
        "field_name",
    ),
)

experiment_passports = Table(
    "experiment_passports",
    metadata,
    Column("passport_id", _ID, primary_key=True),
    Column("experiment_id", _ID, nullable=False),
    Column("experiment_version", Integer, nullable=False),
    Column(
        "validation_id",
        _ID,
        ForeignKey("validation_runs.validation_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("schema_version", String(16), nullable=False),
    Column("supersedes_passport_id", _ID, nullable=True),
    Column("body", JSONB, nullable=False),
    Column(
        "json_object_uri",
        Text,
        ForeignKey("storage_objects.object_uri", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "html_object_uri",
        Text,
        ForeignKey("storage_objects.object_uri", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("json_sha256", _HASH, nullable=False),
    Column("html_sha256", _HASH, nullable=False),
    Column("released_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["experiment_id", "experiment_version"],
        ["experiment_versions.experiment_id", "experiment_versions.version"],
        name="fk_experiment_passports_experiment_version",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["supersedes_passport_id"],
        ["experiment_passports.passport_id"],
        name="fk_experiment_passports_supersedes_passport",
        ondelete="RESTRICT",
    ),
    CheckConstraint("schema_version = '1'", name="known_passport_schema"),
    UniqueConstraint("experiment_id", "experiment_version", name="uq_passport_experiment_version"),
)

experiment_packages = Table(
    "experiment_packages",
    metadata,
    Column("package_id", _ID, primary_key=True),
    Column(
        "passport_id",
        _ID,
        ForeignKey("experiment_passports.passport_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("experiment_id", _ID, nullable=False),
    Column("experiment_version", Integer, nullable=False),
    Column("schema_version", String(16), nullable=False),
    Column("supersedes_package_id", _ID, nullable=True),
    Column(
        "object_uri",
        Text,
        ForeignKey("storage_objects.object_uri", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("archive_sha256", _HASH, nullable=False),
    Column("archive_byte_size", BigInteger, nullable=False),
    Column("body", JSONB, nullable=False),
    *_timestamps("created_at"),
    ForeignKeyConstraint(
        ["experiment_id", "experiment_version"],
        ["experiment_versions.experiment_id", "experiment_versions.version"],
        name="fk_experiment_packages_experiment_version",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["supersedes_package_id"],
        ["experiment_packages.package_id"],
        name="fk_experiment_packages_supersedes_package",
        ondelete="RESTRICT",
    ),
    CheckConstraint("schema_version IN ('1','2','3')", name="known_experiment_package_schema"),
    CheckConstraint("archive_byte_size >= 1", name="experiment_package_not_empty"),
    UniqueConstraint("experiment_id", "experiment_version", name="uq_package_experiment_version"),
)

idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    # The scope is part of the key, not decoration beside it. Two operations that each accept a
    # caller-chosen token would otherwise collide on a token neither of them chose, and the second
    # would be answered with the first one's response.
    Column("scope", String(64), primary_key=True),
    Column("idempotency_key", String(255), primary_key=True),
    #: The canonical request fingerprint (`domain.idempotency.request_fingerprint`). Without it a
    #: key is only a promise: the runtime could not tell a genuine retry from a key reused with a
    #: different body, and would have to guess which of the two the caller meant.
    Column("request_hash", _HASH, nullable=False),
    #: The aggregate the key produced. A column rather than a field inside `response`, so the
    #: reference is typed, indexable, and checkable — the foreign key below is what stops a stored
    #: response from naming a campaign that does not exist.
    Column("campaign_id", UUID(as_uuid=True), nullable=True),
    Column("response", JSONB, nullable=True),
    *_timestamps("created_at"),
    # Deferred on purpose. The reservation is the *first* statement of the submission transaction —
    # that is what makes the uniqueness constraint, rather than a prior read, decide which of two
    # concurrent identical requests creates the campaign. At that point the campaign row does not
    # exist yet, so the reference can only be checked at commit.
    ForeignKeyConstraint(
        ["campaign_id"],
        ["campaigns.campaign_id"],
        name="fk_idempotency_keys_campaign_id_campaigns",
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    ),
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
    "cv_parser_records",
    "cv_structural_findings",
    "cv_transformation_records",
    "derived_metrics",
    "electrolysis_auxiliary_results",
    "electrolysis_import_profiles",
    "electrolysis_structural_findings",
    "electrolysis_transformation_records",
    "events",
    "experiment_packages",
    "experiment_passports",
    "experiment_versions",
    "experiments",
    "idempotency_keys",
    "import_profiles",
    "jobs",
    "metadata",
    "metadata_assertions",
    "normalised_cv_observations",
    "normalised_electrolysis_observations",
    "normalised_observations",
    "observations",
    "record_relations",
    "source_artifacts",
    "storage_objects",
    "validation_findings",
    "validation_runs",
    "work_items",
]

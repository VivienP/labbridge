"""enforce campaign identity on child rows

Autogenerate produced the columns and foreign keys; the primary-key change and every CHECK are
hand-written, because Alembic detects neither. `alembic check` after this revision is what proves
the hand-written half matches the model.

Three defects are closed here, all found by review of the previous revision:

1. `observations.observation_id` was the sole primary key while being content-derived. Two campaigns
   replaying one fixture location produce identical content, so the second receipt either collided —
   losing bytes invariant 2 requires retaining — or was silently attributed to the first campaign's
   attempt. The key becomes `(observation_id, attempt_id)`: one row per receipt, content identity
   preserved.
2. `attempt_outcomes` carried no provenance, origin, or mode, so an outcome with no observation left
   the lineage root recoverable from no row at all.
3. Nothing tied a child row's origin and mode to its campaign's. A `synthetic + replay` observation
   inserted cleanly into an `observed + replay` campaign — both rows individually admissible, the
   conflation happening between them. Composite foreign keys onto `uq_campaigns_identity` close it.

Ordering matters on the way up: the observations primary key must exist before `attempt_outcomes`
can reference it, and the new NOT NULL columns are added nullable, backfilled, then constrained —
there is no production data yet, but a migration that only works on an empty table is not a
migration.

Revision ID: b082c5c8f0f1
Revises: 32ec7ead3c65
Create Date: 2026-07-30 20:03:23.082252+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b082c5c8f0f1"
down_revision: str | None = "32ec7ead3c65"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ADMISSIBLE_PAIR = (
    "(data_origin, execution_mode) IN "
    "(('observed','replay'),('synthetic','replay'),"
    "('synthetic','simulation'),('observed','live'))"
)
_KNOWN_OBSERVATION_STATUS = (
    "status IN ('received','accepted','corrupted','invalidated','superseded')"
)
_KNOWN_ATTEMPT_STATUS = (
    "status IN ('succeeded','timed_out','failed_retryable','failed_terminal',"
    "'corrupted','cancelled','lease_lost','duplicate_suppressed')"
)


def upgrade() -> None:
    # --- 1. observations: composite primary key -------------------------------------------------
    # Every foreign key onto the old single-column key must go first — `derived_metrics` references
    # it too, and PostgreSQL refuses to drop an index another constraint depends on.
    op.drop_constraint(
        op.f("fk_attempt_outcomes_observation_id_observations"),
        "attempt_outcomes",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_derived_metrics_observation_id_observations"),
        "derived_metrics",
        type_="foreignkey",
    )
    op.drop_constraint("uq_derived_metrics_analysis", "derived_metrics", type_="unique")
    op.drop_constraint(op.f("pk_observations"), "observations", type_="primary")
    op.create_primary_key("pk_observations", "observations", ["observation_id", "attempt_id"])
    op.create_check_constraint(
        "ck_observations_known_status", "observations", _KNOWN_OBSERVATION_STATUS
    )

    # A metric is derived from one *receipt*, not from content in the abstract.
    op.add_column("derived_metrics", sa.Column("attempt_id", sa.UUID(), nullable=True))
    op.execute(
        """
        UPDATE derived_metrics AS m
           SET attempt_id = o.attempt_id
          FROM observations AS o
         WHERE o.observation_id = m.observation_id
        """
    )
    op.alter_column("derived_metrics", "attempt_id", nullable=False)
    op.create_foreign_key(
        op.f("fk_derived_metrics_attempt_id_attempts"),
        "derived_metrics",
        "attempts",
        ["attempt_id"],
        ["attempt_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_derived_metrics_observation",
        "derived_metrics",
        "observations",
        ["observation_id", "attempt_id"],
        ["observation_id", "attempt_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_derived_metrics_analysis",
        "derived_metrics",
        [
            "observation_id",
            "attempt_id",
            "name",
            "analysis_name",
            "analysis_version",
            "parameter_hash",
        ],
    )

    # --- 2. attempt_outcomes: provenance and identity -------------------------------------------
    # Added nullable, backfilled from the campaign the outcome's work item belongs to, then made
    # NOT NULL. Adding them NOT NULL outright would fail on any table that already holds rows.
    op.add_column("attempt_outcomes", sa.Column("campaign_id", sa.UUID(), nullable=True))
    op.add_column("attempt_outcomes", sa.Column("data_origin", sa.String(length=16), nullable=True))
    op.add_column(
        "attempt_outcomes", sa.Column("execution_mode", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "attempt_outcomes",
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.execute(
        """
        UPDATE attempt_outcomes AS o
           SET campaign_id = c.campaign_id,
               data_origin = c.data_origin,
               execution_mode = c.execution_mode,
               provenance = '{}'::jsonb
          FROM work_items AS w
          JOIN campaigns AS c ON c.campaign_id = w.campaign_id
         WHERE w.work_item_id = o.work_item_id
        """
    )
    for column in ("campaign_id", "data_origin", "execution_mode", "provenance"):
        op.alter_column("attempt_outcomes", column, nullable=False)

    op.create_check_constraint(
        "ck_attempt_outcomes_known_status", "attempt_outcomes", _KNOWN_ATTEMPT_STATUS
    )
    op.create_check_constraint(
        "ck_attempt_outcomes_admissible_origin_mode", "attempt_outcomes", _ADMISSIBLE_PAIR
    )

    # --- 3. campaign identity as a foreign-key target -------------------------------------------
    op.create_unique_constraint(
        "uq_campaigns_identity", "campaigns", ["campaign_id", "data_origin", "execution_mode"]
    )
    op.drop_constraint(
        op.f("fk_observations_campaign_id_campaigns"), "observations", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_observations_campaign_identity",
        "observations",
        "campaigns",
        ["campaign_id", "data_origin", "execution_mode"],
        ["campaign_id", "data_origin", "execution_mode"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_attempt_outcomes_campaign_identity",
        "attempt_outcomes",
        "campaigns",
        ["campaign_id", "data_origin", "execution_mode"],
        ["campaign_id", "data_origin", "execution_mode"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_attempt_outcomes_observation",
        "attempt_outcomes",
        "observations",
        ["observation_id", "attempt_id"],
        ["observation_id", "attempt_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_attempt_outcomes_observation", "attempt_outcomes", type_="foreignkey")
    op.drop_constraint(
        "fk_attempt_outcomes_campaign_identity", "attempt_outcomes", type_="foreignkey"
    )
    op.drop_constraint("fk_observations_campaign_identity", "observations", type_="foreignkey")
    op.create_foreign_key(
        op.f("fk_observations_campaign_id_campaigns"),
        "observations",
        "campaigns",
        ["campaign_id"],
        ["campaign_id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint("uq_campaigns_identity", "campaigns", type_="unique")

    op.drop_constraint(
        "ck_attempt_outcomes_admissible_origin_mode", "attempt_outcomes", type_="check"
    )
    op.drop_constraint("ck_attempt_outcomes_known_status", "attempt_outcomes", type_="check")
    for column in ("provenance", "execution_mode", "data_origin", "campaign_id"):
        op.drop_column("attempt_outcomes", column)

    op.drop_constraint("uq_derived_metrics_analysis", "derived_metrics", type_="unique")
    op.drop_constraint("fk_derived_metrics_observation", "derived_metrics", type_="foreignkey")
    op.drop_constraint(
        op.f("fk_derived_metrics_attempt_id_attempts"), "derived_metrics", type_="foreignkey"
    )
    op.drop_column("derived_metrics", "attempt_id")

    op.drop_constraint("ck_observations_known_status", "observations", type_="check")
    op.drop_constraint(op.f("pk_observations"), "observations", type_="primary")
    op.create_primary_key("pk_observations", "observations", ["observation_id"])
    op.create_foreign_key(
        op.f("fk_attempt_outcomes_observation_id_observations"),
        "attempt_outcomes",
        "observations",
        ["observation_id"],
        ["observation_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_derived_metrics_observation_id_observations"),
        "derived_metrics",
        "observations",
        ["observation_id"],
        ["observation_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_derived_metrics_analysis",
        "derived_metrics",
        ["observation_id", "name", "analysis_name", "analysis_version", "parameter_hash"],
    )

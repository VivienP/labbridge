"""add source artifacts

Revision ID: f1206c4b9a01
Revises: 1e6a158aabea
Create Date: 2026-08-12 10:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1206c4b9a01"
down_revision: str | None = "1e6a158aabea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ADMISSIBLE_PAIR = (
    "(data_origin, execution_mode) IN "
    "(('observed','replay'),('synthetic','replay'),"
    "('synthetic','simulation'),('observed','live'))"
)


def upgrade() -> None:
    # Earlier revisions supplied already-prefixed names to a naming convention, which produces a
    # double prefix on a database created from scratch. Existing upgraded databases may already
    # carry the canonical names, so each correction is conditional.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = 'ck_attempt_outcomes_ck_attempt_outcomes_admissible_origin_mode'
          ) THEN
            ALTER TABLE attempt_outcomes RENAME CONSTRAINT
              ck_attempt_outcomes_ck_attempt_outcomes_admissible_origin_mode
              TO ck_attempt_outcomes_admissible_origin_mode;
          END IF;
          IF EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = 'ck_attempt_outcomes_ck_attempt_outcomes_known_status'
          ) THEN
            ALTER TABLE attempt_outcomes RENAME CONSTRAINT
              ck_attempt_outcomes_ck_attempt_outcomes_known_status
              TO ck_attempt_outcomes_known_status;
          END IF;
          IF EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = 'ck_observations_ck_observations_known_status'
          ) THEN
            ALTER TABLE observations RENAME CONSTRAINT
              ck_observations_ck_observations_known_status
              TO ck_observations_known_status;
          END IF;
        END $$;
        """
    )
    op.create_table(
        "source_artifacts",
        sa.Column("source_artifact_id", sa.String(length=128), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("data_origin", sa.String(length=16), nullable=False),
        sa.Column("execution_mode", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("object_uri", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quarantine_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(state = 'committed') = (committed_at IS NOT NULL)",
            name=op.f("ck_source_artifacts_committed_source_has_timestamp"),
        ),
        sa.CheckConstraint(
            "state IN ('pending','committed','quarantined')",
            name=op.f("ck_source_artifacts_known_source_state"),
        ),
        sa.CheckConstraint(
            "(state = 'quarantined') = (quarantine_reason IS NOT NULL)",
            name=op.f("ck_source_artifacts_quarantined_source_has_reason"),
        ),
        sa.CheckConstraint(
            _ADMISSIBLE_PAIR,
            name=op.f("ck_source_artifacts_source_admissible_origin_mode"),
        ),
        sa.CheckConstraint(
            "byte_size >= 0", name=op.f("ck_source_artifacts_source_byte_size_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["object_uri"],
            ["storage_objects.object_uri"],
            name=op.f("fk_source_artifacts_object_uri_storage_objects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("source_artifact_id", name=op.f("pk_source_artifacts")),
    )


def downgrade() -> None:
    op.drop_table("source_artifacts")
    # Restore the names the historical downgrade targets before control returns to that revision.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = 'ck_attempt_outcomes_admissible_origin_mode'
          ) THEN
            ALTER TABLE attempt_outcomes RENAME CONSTRAINT
              ck_attempt_outcomes_admissible_origin_mode
              TO ck_attempt_outcomes_ck_attempt_outcomes_admissible_origin_mode;
          END IF;
          IF EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = 'ck_attempt_outcomes_known_status'
          ) THEN
            ALTER TABLE attempt_outcomes RENAME CONSTRAINT
              ck_attempt_outcomes_known_status
              TO ck_attempt_outcomes_ck_attempt_outcomes_known_status;
          END IF;
          IF EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = 'ck_observations_known_status'
          ) THEN
            ALTER TABLE observations RENAME CONSTRAINT
              ck_observations_known_status
              TO ck_observations_ck_observations_known_status;
          END IF;
        END $$;
        """
    )

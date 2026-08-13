"""add retained CV parser records

Revision ID: 61d3f47b809a
Revises: c4d9a7e21b63
Create Date: 2026-08-12 23:50:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "61d3f47b809a"
down_revision: str | None = "c4d9a7e21b63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cv_parser_records",
        sa.Column("parser_record_id", sa.String(length=128), nullable=False),
        sa.Column("source_artifact_id", sa.String(length=128), nullable=False),
        sa.Column("profile_id", sa.String(length=128), nullable=False),
        sa.Column("observation_id", sa.String(length=128), nullable=True),
        sa.Column("source_format", sa.String(length=32), nullable=False),
        sa.Column("parser_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_format IN ('generic_csv','gamry_dta')",
            name=op.f("ck_cv_parser_records_known_source_format"),
        ),
        sa.CheckConstraint(
            "status IN ('accepted','rejected')",
            name=op.f("ck_cv_parser_records_known_parser_status"),
        ),
        sa.CheckConstraint(
            "(status = 'accepted' AND observation_id IS NOT NULL) OR "
            "(status = 'rejected' AND observation_id IS NULL)",
            name=op.f("ck_cv_parser_records_parser_status_matches_observation"),
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["normalised_cv_observations.observation_id"],
            name=op.f("fk_cv_parser_records_observation_id_normalised_cv_observations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["import_profiles.profile_id"],
            name=op.f("fk_cv_parser_records_profile_id_import_profiles"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_artifact_id"],
            ["source_artifacts.source_artifact_id"],
            name=op.f("fk_cv_parser_records_source_artifact_id_source_artifacts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("parser_record_id", name=op.f("pk_cv_parser_records")),
        sa.UniqueConstraint("observation_id", name=op.f("uq_cv_parser_records_observation_id")),
    )
    op.drop_constraint(
        op.f("ck_experiment_packages_known_experiment_package_schema"),
        "experiment_packages",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_experiment_packages_known_experiment_package_schema"),
        "experiment_packages",
        "schema_version IN ('1','2')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_experiment_packages_known_experiment_package_schema"),
        "experiment_packages",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_experiment_packages_known_experiment_package_schema"),
        "experiment_packages",
        "schema_version = '1'",
    )
    op.drop_table("cv_parser_records")

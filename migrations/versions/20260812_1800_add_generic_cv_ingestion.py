"""add generic CV ingestion

Revision ID: 8b8e4e2d9f31
Revises: f1206c4b9a01
Create Date: 2026-08-12 18:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8b8e4e2d9f31"
down_revision: str | None = "f1206c4b9a01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ADMISSIBLE_PAIR = (
    "(data_origin, execution_mode) IN "
    "(('observed','replay'),('synthetic','replay'),"
    "('synthetic','simulation'),('observed','live'))"
)


def upgrade() -> None:
    op.create_table(
        "import_profiles",
        sa.Column("profile_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("technique", sa.String(length=64), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = '1'", name=op.f("ck_import_profiles_known_import_profile_schema")
        ),
        sa.CheckConstraint(
            "technique = 'cyclic_voltammetry'",
            name=op.f("ck_import_profiles_known_import_profile_technique"),
        ),
        sa.PrimaryKeyConstraint("profile_id", name=op.f("pk_import_profiles")),
    )
    op.create_table(
        "normalised_cv_observations",
        sa.Column("observation_id", sa.String(length=128), nullable=False),
        sa.Column("source_artifact_id", sa.String(length=128), nullable=False),
        sa.Column("profile_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("parser_version", sa.String(length=64), nullable=False),
        sa.Column("normalisation_version", sa.String(length=64), nullable=False),
        sa.Column("data_origin", sa.String(length=16), nullable=False),
        sa.Column("execution_mode", sa.String(length=16), nullable=False),
        sa.Column("environment_id", sa.String(length=128), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("object_uri", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "byte_size >= 0",
            name=op.f("ck_normalised_cv_observations_normalised_cv_byte_size_non_negative"),
        ),
        sa.CheckConstraint(
            _ADMISSIBLE_PAIR,
            name=op.f("ck_normalised_cv_observations_normalised_cv_admissible_origin_mode"),
        ),
        sa.CheckConstraint(
            "row_count >= 1", name=op.f("ck_normalised_cv_observations_normalised_cv_has_rows")
        ),
        sa.CheckConstraint(
            "schema_version = '1'",
            name=op.f("ck_normalised_cv_observations_known_normalised_cv_schema"),
        ),
        sa.ForeignKeyConstraint(
            ["object_uri"],
            ["storage_objects.object_uri"],
            name=op.f("fk_normalised_cv_observations_object_uri_storage_objects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["import_profiles.profile_id"],
            name=op.f("fk_normalised_cv_observations_profile_id_import_profiles"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_artifact_id"],
            ["source_artifacts.source_artifact_id"],
            name=op.f("fk_normalised_cv_observations_source_artifact_id_source_artifacts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("observation_id", name=op.f("pk_normalised_cv_observations")),
    )
    op.create_table(
        "cv_structural_findings",
        sa.Column("finding_id", sa.String(length=128), nullable=False),
        sa.Column("observation_id", sa.String(length=128), nullable=False),
        sa.Column("finding", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["normalised_cv_observations.observation_id"],
            name=op.f("fk_cv_structural_findings_observation_id_normalised_cv_observations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("finding_id", name=op.f("pk_cv_structural_findings")),
    )
    op.create_table(
        "cv_transformation_records",
        sa.Column("transformation_id", sa.String(length=128), nullable=False),
        sa.Column("observation_id", sa.String(length=128), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("record", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 1",
            name=op.f("ck_cv_transformation_records_cv_transform_ordinal_starts_at_one"),
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["normalised_cv_observations.observation_id"],
            name=op.f("fk_cv_transformation_records_observation_id_normalised_cv_observations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("transformation_id", name=op.f("pk_cv_transformation_records")),
        sa.UniqueConstraint(
            "observation_id", "ordinal", name=op.f("uq_cv_transform_observation_ordinal")
        ),
    )


def downgrade() -> None:
    op.drop_table("cv_transformation_records")
    op.drop_table("cv_structural_findings")
    op.drop_table("normalised_cv_observations")
    op.drop_table("import_profiles")

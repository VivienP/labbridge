"""add Experiment Passports and verified Packages

Revision ID: c4d9a7e21b63
Revises: 8b8e4e2d9f31
Create Date: 2026-08-12 23:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4d9a7e21b63"
down_revision: str | None = "8b8e4e2d9f31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ADMISSIBLE_PAIR = (
    "(data_origin, execution_mode) IN "
    "(('observed','replay'),('synthetic','replay'),"
    "('synthetic','simulation'),('observed','live'))"
)


def upgrade() -> None:
    op.create_table(
        "experiments",
        sa.Column("experiment_id", sa.String(length=128), nullable=False),
        sa.Column("observation_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("technique", sa.String(length=64), nullable=False),
        sa.Column("data_origin", sa.String(length=16), nullable=False),
        sa.Column("execution_mode", sa.String(length=16), nullable=False),
        sa.Column("environment_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            _ADMISSIBLE_PAIR, name=op.f("ck_experiments_experiment_admissible_origin_mode")
        ),
        sa.CheckConstraint(
            "current_version >= 1",
            name=op.f("ck_experiments_experiment_version_starts_at_one"),
        ),
        sa.CheckConstraint(
            "schema_version = '1'", name=op.f("ck_experiments_known_experiment_schema")
        ),
        sa.CheckConstraint(
            "technique = 'cyclic_voltammetry'",
            name=op.f("ck_experiments_known_experiment_technique"),
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["normalised_cv_observations.observation_id"],
            name=op.f("fk_experiments_observation_id_normalised_cv_observations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("experiment_id", name=op.f("pk_experiments")),
        sa.UniqueConstraint("observation_id", name=op.f("uq_experiments_observation_id")),
    )
    op.create_table(
        "experiment_versions",
        sa.Column("experiment_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("supersedes_version", sa.Integer(), nullable=True),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(version = 1 AND supersedes_version IS NULL) OR "
            "(version > 1 AND supersedes_version = version - 1)",
            name=op.f("ck_experiment_versions_experiment_snapshot_supersedes_predecessor"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_experiment_versions_experiment_snapshot_version_starts_at_one"),
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.experiment_id"],
            name="fk_experiment_versions_experiment_id_experiments",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id", "supersedes_version"],
            ["experiment_versions.experiment_id", "experiment_versions.version"],
            name="fk_experiment_versions_superseded_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("experiment_id", "version", name="pk_experiment_versions"),
    )
    op.create_table(
        "metadata_assertions",
        sa.Column("assertion_id", sa.String(length=128), nullable=False),
        sa.Column("experiment_id", sa.String(length=128), nullable=False),
        sa.Column("created_version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("field_name", sa.String(length=128), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("transformation", sa.String(length=32), nullable=False),
        sa.Column("requirement_class", sa.String(length=32), nullable=False),
        sa.Column("value_state", sa.String(length=32), nullable=False),
        sa.Column("supplements_assertion_id", sa.String(length=128), nullable=True),
        sa.Column("supersedes_assertion_id", sa.String(length=128), nullable=True),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "created_version >= 1",
            name=op.f("ck_metadata_assertions_assertion_version_starts_at_one"),
        ),
        sa.CheckConstraint(
            "origin IN ('source_file','user_supplied','inferred')",
            name=op.f("ck_metadata_assertions_known_assertion_origin"),
        ),
        sa.CheckConstraint(
            "requirement_class IN ('required','conditional','recommended','optional')",
            name=op.f("ck_metadata_assertions_known_assertion_requirement_class"),
        ),
        sa.CheckConstraint(
            "schema_version = '1'",
            name=op.f("ck_metadata_assertions_known_metadata_assertion_schema"),
        ),
        sa.CheckConstraint(
            "transformation IN ('none','parsed','unit_converted','derived')",
            name=op.f("ck_metadata_assertions_known_assertion_transformation"),
        ),
        sa.CheckConstraint(
            "value_state IN ('known','unknown','unavailable','not_applicable')",
            name=op.f("ck_metadata_assertions_known_assertion_value_state"),
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id", "created_version"],
            ["experiment_versions.experiment_id", "experiment_versions.version"],
            name="fk_metadata_assertions_experiment_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supplements_assertion_id"],
            ["metadata_assertions.assertion_id"],
            name="fk_metadata_assertions_supplements_assertion",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_assertion_id"],
            ["metadata_assertions.assertion_id"],
            name="fk_metadata_assertions_supersedes_assertion",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("assertion_id", name=op.f("pk_metadata_assertions")),
    )
    op.create_index(
        "ix_metadata_assertions_query_dimensions",
        "metadata_assertions",
        [
            "experiment_id",
            "field_name",
            "origin",
            "transformation",
            "requirement_class",
            "value_state",
        ],
        unique=False,
    )
    op.create_table(
        "validation_runs",
        sa.Column("validation_id", sa.String(length=128), nullable=False),
        sa.Column("experiment_id", sa.String(length=128), nullable=False),
        sa.Column("experiment_version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("validation_version", sa.String(length=64), nullable=False),
        sa.Column("release_status", sa.String(length=16), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "release_status IN ('blocked','eligible')",
            name=op.f("ck_validation_runs_known_release_decision"),
        ),
        sa.CheckConstraint(
            "schema_version = '1'", name=op.f("ck_validation_runs_known_validation_schema")
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id", "experiment_version"],
            ["experiment_versions.experiment_id", "experiment_versions.version"],
            name="fk_validation_runs_experiment_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("validation_id", name=op.f("pk_validation_runs")),
    )
    op.create_table(
        "validation_findings",
        sa.Column("finding_id", sa.String(length=128), nullable=False),
        sa.Column("validation_id", sa.String(length=128), nullable=False),
        sa.Column("experiment_id", sa.String(length=128), nullable=False),
        sa.Column("field_name", sa.String(length=128), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("requirement_class", sa.String(length=32), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "requirement_class IN ('required','conditional','recommended','optional')",
            name=op.f("ck_validation_findings_known_finding_requirement_class"),
        ),
        sa.CheckConstraint(
            "severity IN ('blocking','warning','unknown')",
            name=op.f("ck_validation_findings_known_finding_severity"),
        ),
        sa.ForeignKeyConstraint(
            ["validation_id"],
            ["validation_runs.validation_id"],
            name=op.f("fk_validation_findings_validation_id_validation_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("finding_id", name=op.f("pk_validation_findings")),
    )
    op.create_index(
        "ix_validation_findings_query_dimensions",
        "validation_findings",
        ["experiment_id", "severity", "requirement_class", "field_name"],
        unique=False,
    )
    op.create_table(
        "experiment_passports",
        sa.Column("passport_id", sa.String(length=128), nullable=False),
        sa.Column("experiment_id", sa.String(length=128), nullable=False),
        sa.Column("experiment_version", sa.Integer(), nullable=False),
        sa.Column("validation_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("supersedes_passport_id", sa.String(length=128), nullable=True),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("json_object_uri", sa.Text(), nullable=False),
        sa.Column("html_object_uri", sa.Text(), nullable=False),
        sa.Column("json_sha256", sa.String(length=64), nullable=False),
        sa.Column("html_sha256", sa.String(length=64), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = '1'", name=op.f("ck_experiment_passports_known_passport_schema")
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id", "experiment_version"],
            ["experiment_versions.experiment_id", "experiment_versions.version"],
            name="fk_experiment_passports_experiment_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["html_object_uri"],
            ["storage_objects.object_uri"],
            name=op.f("fk_experiment_passports_html_object_uri_storage_objects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["json_object_uri"],
            ["storage_objects.object_uri"],
            name=op.f("fk_experiment_passports_json_object_uri_storage_objects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_passport_id"],
            ["experiment_passports.passport_id"],
            name="fk_experiment_passports_supersedes_passport",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["validation_id"],
            ["validation_runs.validation_id"],
            name=op.f("fk_experiment_passports_validation_id_validation_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("passport_id", name=op.f("pk_experiment_passports")),
        sa.UniqueConstraint(
            "experiment_id", "experiment_version", name="uq_passport_experiment_version"
        ),
    )
    op.create_table(
        "experiment_packages",
        sa.Column("package_id", sa.String(length=128), nullable=False),
        sa.Column("passport_id", sa.String(length=128), nullable=False),
        sa.Column("experiment_id", sa.String(length=128), nullable=False),
        sa.Column("experiment_version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("supersedes_package_id", sa.String(length=128), nullable=True),
        sa.Column("object_uri", sa.Text(), nullable=False),
        sa.Column("archive_sha256", sa.String(length=64), nullable=False),
        sa.Column("archive_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "archive_byte_size >= 1",
            name=op.f("ck_experiment_packages_experiment_package_not_empty"),
        ),
        sa.CheckConstraint(
            "schema_version = '1'",
            name=op.f("ck_experiment_packages_known_experiment_package_schema"),
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id", "experiment_version"],
            ["experiment_versions.experiment_id", "experiment_versions.version"],
            name="fk_experiment_packages_experiment_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["object_uri"],
            ["storage_objects.object_uri"],
            name=op.f("fk_experiment_packages_object_uri_storage_objects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["passport_id"],
            ["experiment_passports.passport_id"],
            name=op.f("fk_experiment_packages_passport_id_experiment_passports"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_package_id"],
            ["experiment_packages.package_id"],
            name="fk_experiment_packages_supersedes_package",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("package_id", name=op.f("pk_experiment_packages")),
        sa.UniqueConstraint(
            "experiment_id", "experiment_version", name="uq_package_experiment_version"
        ),
    )


def downgrade() -> None:
    op.drop_table("experiment_packages")
    op.drop_table("experiment_passports")
    op.drop_index("ix_validation_findings_query_dimensions", table_name="validation_findings")
    op.drop_table("validation_findings")
    op.drop_table("validation_runs")
    op.drop_index("ix_metadata_assertions_query_dimensions", table_name="metadata_assertions")
    op.drop_table("metadata_assertions")
    op.drop_table("experiment_versions")
    op.drop_table("experiments")

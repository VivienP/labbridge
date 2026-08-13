"""Add the bounded galvanostatic electrolysis package persistence.

Revision ID: 9a4e8f2c71d0
Revises: 61d3f47b809a
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9a4e8f2c71d0"
down_revision: str | None = "61d3f47b809a"
branch_labels: str | None = None
depends_on: str | None = None

_ADMISSIBLE_PAIR = (
    "(data_origin = 'observed' AND execution_mode IN ('replay','live')) OR "
    "(data_origin = 'synthetic' AND execution_mode = 'simulation') OR "
    "(data_origin = 'synthetic' AND execution_mode = 'replay')"
)


def upgrade() -> None:
    op.create_table(
        "normalised_observations",
        sa.Column("observation_id", sa.String(length=128), nullable=False),
        sa.Column("technique", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "technique IN ('cyclic_voltammetry','galvanostatic_electrolysis')",
            name=op.f("ck_normalised_observations_known_normalised_observation_technique"),
        ),
        sa.PrimaryKeyConstraint("observation_id", name=op.f("pk_normalised_observations")),
    )
    op.execute(
        "INSERT INTO normalised_observations (observation_id, technique, created_at) "
        "SELECT observation_id, 'cyclic_voltammetry', created_at "
        "FROM normalised_cv_observations"
    )
    op.create_foreign_key(
        op.f("fk_normalised_cv_observations_observation_id_normalised_observations"),
        "normalised_cv_observations",
        "normalised_observations",
        ["observation_id"],
        ["observation_id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "fk_experiments_observation_id_normalised_cv_observations",
        "experiments",
        type_="foreignkey",
    )
    op.create_foreign_key(
        op.f("fk_experiments_observation_id_normalised_observations"),
        "experiments",
        "normalised_observations",
        ["observation_id"],
        ["observation_id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        op.f("ck_experiments_known_experiment_technique"), "experiments", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_experiments_known_experiment_technique"),
        "experiments",
        "technique IN ('cyclic_voltammetry','galvanostatic_electrolysis')",
    )
    op.drop_constraint(
        op.f("ck_experiment_packages_known_experiment_package_schema"),
        "experiment_packages",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_experiment_packages_known_experiment_package_schema"),
        "experiment_packages",
        "schema_version IN ('1','2','3')",
    )
    op.create_table(
        "electrolysis_import_profiles",
        sa.Column("profile_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("technique", sa.String(length=64), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = '1'",
            name=op.f("ck_electrolysis_import_profiles_known_electrolysis_profile_schema"),
        ),
        sa.CheckConstraint(
            "technique = 'galvanostatic_electrolysis'",
            name=op.f("ck_electrolysis_import_profiles_known_electrolysis_profile_technique"),
        ),
        sa.PrimaryKeyConstraint("profile_id", name=op.f("pk_electrolysis_import_profiles")),
    )
    op.create_table(
        "normalised_electrolysis_observations",
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
            _ADMISSIBLE_PAIR,
            name=op.f(
                "ck_normalised_electrolysis_observations_electrolysis_observation_admissible_origin_mode"
            ),
        ),
        sa.CheckConstraint(
            "byte_size >= 0",
            name=op.f(
                "ck_normalised_electrolysis_observations_electrolysis_observation_byte_size_non_negative"
            ),
        ),
        sa.CheckConstraint(
            "row_count >= 1",
            name=op.f("ck_normalised_electrolysis_observations_electrolysis_observation_has_rows"),
        ),
        sa.CheckConstraint(
            "schema_version = '1'",
            name=op.f(
                "ck_normalised_electrolysis_observations_known_electrolysis_observation_schema"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["object_uri"],
            ["storage_objects.object_uri"],
            name=op.f("fk_normalised_electrolysis_observations_object_uri_storage_objects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["normalised_observations.observation_id"],
            name=op.f(
                "fk_normalised_electrolysis_observations_observation_id_normalised_observations"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["electrolysis_import_profiles.profile_id"],
            name=op.f(
                "fk_normalised_electrolysis_observations_profile_id_electrolysis_import_profiles"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_artifact_id"],
            ["source_artifacts.source_artifact_id"],
            name=op.f(
                "fk_normalised_electrolysis_observations_source_artifact_id_source_artifacts"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "observation_id", name=op.f("pk_normalised_electrolysis_observations")
        ),
    )
    op.create_table(
        "electrolysis_transformation_records",
        sa.Column("transformation_id", sa.String(length=128), nullable=False),
        sa.Column("observation_id", sa.String(length=128), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("record", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 1",
            name=op.f(
                "ck_electrolysis_transformation_records_electrolysis_transform_ordinal_starts_at_one"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["normalised_electrolysis_observations.observation_id"],
            name=op.f(
                "fk_electrolysis_transformation_records_observation_id_normalised_electrolysis_observations"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "transformation_id", name=op.f("pk_electrolysis_transformation_records")
        ),
        sa.UniqueConstraint(
            "observation_id",
            "ordinal",
            name=op.f("uq_electrolysis_transform_observation_ordinal"),
        ),
    )
    op.create_table(
        "electrolysis_structural_findings",
        sa.Column("finding_id", sa.String(length=128), nullable=False),
        sa.Column("observation_id", sa.String(length=128), nullable=False),
        sa.Column("finding", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["normalised_electrolysis_observations.observation_id"],
            name=op.f(
                "fk_electrolysis_structural_findings_observation_id_normalised_electrolysis_observations"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("finding_id", name=op.f("pk_electrolysis_structural_findings")),
    )
    op.create_table(
        "electrolysis_auxiliary_results",
        sa.Column("result_id", sa.String(length=128), nullable=False),
        sa.Column("observation_id", sa.String(length=128), nullable=False),
        sa.Column("source_artifact_id", sa.String(length=128), nullable=False),
        sa.Column("method_name", sa.String(length=128), nullable=False),
        sa.Column("method_version", sa.String(length=64), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["normalised_electrolysis_observations.observation_id"],
            name=op.f(
                "fk_electrolysis_auxiliary_results_observation_id_normalised_electrolysis_observations"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_artifact_id"],
            ["source_artifacts.source_artifact_id"],
            name=op.f("fk_electrolysis_auxiliary_results_source_artifact_id_source_artifacts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("result_id", name=op.f("pk_electrolysis_auxiliary_results")),
    )


def downgrade() -> None:
    connection = op.get_bind()
    retained = connection.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM normalised_electrolysis_observations) OR "
            "EXISTS (SELECT 1 FROM experiment_packages WHERE schema_version = '3')"
        )
    ).scalar_one()
    if retained:
        raise RuntimeError("cannot downgrade while electrolysis observations or Packages exist")
    op.drop_table("electrolysis_auxiliary_results")
    op.drop_table("electrolysis_structural_findings")
    op.drop_table("electrolysis_transformation_records")
    op.drop_table("normalised_electrolysis_observations")
    op.drop_table("electrolysis_import_profiles")
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
    op.drop_constraint(
        op.f("ck_experiments_known_experiment_technique"), "experiments", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_experiments_known_experiment_technique"),
        "experiments",
        "technique = 'cyclic_voltammetry'",
    )
    op.drop_constraint(
        op.f("fk_experiments_observation_id_normalised_observations"),
        "experiments",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_experiments_observation_id_normalised_cv_observations",
        "experiments",
        "normalised_cv_observations",
        ["observation_id"],
        ["observation_id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        op.f("fk_normalised_cv_observations_observation_id_normalised_observations"),
        "normalised_cv_observations",
        type_="foreignkey",
    )
    op.drop_table("normalised_observations")

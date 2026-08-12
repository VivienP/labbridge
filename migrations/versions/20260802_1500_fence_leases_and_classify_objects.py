"""Fence leases, and give stored objects an identity and a verdict.

Three groups of change, all additive:

* `jobs.lease_generation` — the monotonic fencing token. Existing rows start at zero and the first
  claim after this migration moves them to one, so no live holder is fenced out by the migration
  itself;
* `storage_objects` gains the identity a reconciler needs to attribute an orphan, and the columns a
  classification is recorded in;
* `observations` gains a partial unique index making at most one accepted receipt per work item a
  database guarantee, and `attempt_outcomes` may now reference a receipt that was refused.

The accepted-observation index is created only after checking that no work item already has two.
A migration that fails halfway through building a unique index leaves a schema nobody can reason
about, and the check turns that into a refusal with the offending rows named.

Revision ID: c7a41f8d5b02
Revises: b3f18d4c07ae
Create Date: 2026-08-02 15:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7a41f8d5b02"
down_revision: str | None = "b3f18d4c07ae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KNOWN_CLASSIFICATION = (
    "classification IS NULL OR classification IN "
    "('accepted_evidence','diagnostic_duplicate','diagnostic_orphan','quarantined','missing')"
)


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("lease_generation", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.create_check_constraint("lease_generation_non_negative", "jobs", "lease_generation >= 0")
    op.create_check_constraint(
        "lease_owner_and_token_together",
        "jobs",
        "(lease_owner IS NULL) = (lease_token IS NULL)",
    )

    op.add_column("storage_objects", sa.Column("media_type", sa.String(length=128), nullable=True))
    op.add_column(
        "storage_objects", sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "storage_objects", sa.Column("work_item_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "storage_objects", sa.Column("classification", sa.String(length=32), nullable=True)
    )
    op.add_column("storage_objects", sa.Column("classification_reason", sa.Text(), nullable=True))
    op.add_column(
        "storage_objects",
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_storage_objects_attempt_id_attempts",
        "storage_objects",
        "attempts",
        ["attempt_id"],
        ["attempt_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_storage_objects_work_item_id_work_items",
        "storage_objects",
        "work_items",
        ["work_item_id"],
        ["work_item_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint("known_classification", "storage_objects", _KNOWN_CLASSIFICATION)
    op.create_check_constraint(
        "classification_records_when_it_was_reached",
        "storage_objects",
        "(classification IS NULL) = (reconciled_at IS NULL)",
    )
    op.create_check_constraint(
        "classification_states_its_reason",
        "storage_objects",
        "classification IS NULL OR classification_reason IS NOT NULL",
    )

    op.create_check_constraint(
        "retained_receipt_states_its_reason",
        "observations",
        "status <> 'received' OR status_reason IS NOT NULL",
    )

    connection = op.get_bind()
    conflicting = connection.execute(
        sa.text(
            """
            SELECT count(*) FROM (
                SELECT work_item_id FROM observations
                WHERE status = 'accepted'
                GROUP BY work_item_id HAVING count(*) > 1
            ) AS duplicated
            """
        )
    ).scalar_one()
    if conflicting:
        raise RuntimeError(
            f"{conflicting} work item(s) already hold more than one accepted observation; "
            "resolve them before the uniqueness constraint can be enforced"
        )
    op.create_index(
        "uq_observations_one_accepted_per_work_item",
        "observations",
        ["work_item_id"],
        unique=True,
        postgresql_where=sa.text("status = 'accepted'"),
    )

    op.drop_constraint(
        op.f("ck_attempt_outcomes_observation_only_when_bytes_arrived"),
        "attempt_outcomes",
        type_="check",
    )
    op.create_check_constraint(
        "observation_only_when_bytes_arrived",
        "attempt_outcomes",
        "observation_id IS NULL "
        "OR status IN ('succeeded','corrupted','duplicate_suppressed','lease_lost')",
    )


def downgrade() -> None:
    connection = op.get_bind()
    # Narrowing the check would orphan the references it currently permits. Refusing names the rows
    # rather than silently dropping the link between a refused execution and the bytes it retained.
    retained = connection.execute(
        sa.text(
            "SELECT count(*) FROM attempt_outcomes WHERE observation_id IS NOT NULL "
            "AND status IN ('duplicate_suppressed','lease_lost')"
        )
    ).scalar_one()
    if retained:
        raise RuntimeError(
            f"{retained} refused outcome(s) reference retained diagnostic bytes; narrowing the "
            "constraint would leave those references unrepresentable"
        )

    op.drop_constraint(
        op.f("ck_attempt_outcomes_observation_only_when_bytes_arrived"),
        "attempt_outcomes",
        type_="check",
    )
    op.create_check_constraint(
        "observation_only_when_bytes_arrived",
        "attempt_outcomes",
        "observation_id IS NULL OR status IN ('succeeded','corrupted')",
    )
    op.drop_index("uq_observations_one_accepted_per_work_item", table_name="observations")
    op.drop_constraint(
        op.f("ck_observations_retained_receipt_states_its_reason"), "observations", type_="check"
    )
    op.drop_constraint(
        op.f("ck_storage_objects_classification_states_its_reason"),
        "storage_objects",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_storage_objects_classification_records_when_it_was_reached"),
        "storage_objects",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_storage_objects_known_classification"), "storage_objects", type_="check"
    )
    op.drop_constraint(
        "fk_storage_objects_work_item_id_work_items", "storage_objects", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_storage_objects_attempt_id_attempts", "storage_objects", type_="foreignkey"
    )
    op.drop_column("storage_objects", "reconciled_at")
    op.drop_column("storage_objects", "classification_reason")
    op.drop_column("storage_objects", "classification")
    op.drop_column("storage_objects", "work_item_id")
    op.drop_column("storage_objects", "attempt_id")
    op.drop_column("storage_objects", "media_type")
    op.drop_constraint(op.f("ck_jobs_lease_owner_and_token_together"), "jobs", type_="check")
    op.drop_constraint(op.f("ck_jobs_lease_generation_non_negative"), "jobs", type_="check")
    op.drop_column("jobs", "lease_generation")

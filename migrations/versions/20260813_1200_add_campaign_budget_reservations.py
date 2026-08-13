"""Add transactional campaign budget reservations.

Revision ID: 74e1b6a09d22
Revises: 61d3f47b809a
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "74e1b6a09d22"
down_revision: str | None = "61d3f47b809a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_budget_ledger_known_ledger_kind"), "budget_ledger", type_="check")
    op.create_check_constraint(
        op.f("ck_budget_ledger_known_ledger_kind"),
        "budget_ledger",
        "kind IN ('reserved','consumed','released','adjusted_up','adjusted_down')",
    )
    op.add_column(
        "campaigns",
        sa.Column(
            "hard_budget",
            sa.Numeric(),
            server_default=sa.text("999999999999999999"),
            nullable=False,
        ),
    )
    op.add_column(
        "campaigns",
        sa.Column(
            "per_attempt_estimate", sa.Numeric(), server_default=sa.text("1"), nullable=False
        ),
    )
    op.add_column(
        "campaigns",
        sa.Column(
            "budget_unit",
            sa.String(length=32),
            server_default=sa.text("'attempt'"),
            nullable=False,
        ),
    )
    op.add_column(
        "campaigns",
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False),
    )
    op.add_column(
        "campaigns",
        sa.Column(
            "stopping_rule",
            sa.String(length=64),
            server_default=sa.text("'hard_budget_exhausted'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_campaigns_hard_budget_positive"), "campaigns", "hard_budget > 0"
    )
    op.create_check_constraint(
        op.f("ck_campaigns_per_attempt_estimate_positive"),
        "campaigns",
        "per_attempt_estimate > 0",
    )
    op.create_check_constraint(
        op.f("ck_campaigns_attempt_estimate_within_hard_budget"),
        "campaigns",
        "per_attempt_estimate <= hard_budget",
    )
    op.create_check_constraint(
        op.f("ck_campaigns_budget_unit_present"),
        "campaigns",
        "length(btrim(budget_unit)) > 0",
    )
    op.create_check_constraint(
        op.f("ck_campaigns_max_attempts_positive"), "campaigns", "max_attempts >= 1"
    )
    op.create_check_constraint(
        op.f("ck_campaigns_known_stopping_rule"),
        "campaigns",
        "stopping_rule = 'hard_budget_exhausted'",
    )

    op.add_column(
        "attempts", sa.Column("adapter_started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_unique_constraint(
        "uq_work_items_campaign_identity", "work_items", ["work_item_id", "campaign_id"]
    )
    op.create_unique_constraint("uq_jobs_work_item_identity", "jobs", ["job_id", "work_item_id"])
    op.create_unique_constraint(
        "uq_attempts_execution_identity",
        "attempts",
        ["attempt_id", "work_item_id", "job_id"],
    )

    op.add_column("budget_ledger", sa.Column("job_id", postgresql.UUID(), nullable=True))
    op.add_column("budget_ledger", sa.Column("attempt_id", postgresql.UUID(), nullable=True))
    op.add_column("budget_ledger", sa.Column("lease_generation", sa.BigInteger(), nullable=True))
    op.add_column(
        "budget_ledger",
        sa.Column("reservation_entry_id", postgresql.UUID(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_budget_ledger_reservation_identity",
        "budget_ledger",
        ["entry_id", "campaign_id", "work_item_id", "job_id", "lease_generation"],
    )
    op.execute(
        "ALTER TABLE budget_ledger ADD CONSTRAINT ck_budget_ledger_ledger_amount_positive "
        "CHECK (amount > 0) NOT VALID"
    )
    op.execute(
        "ALTER TABLE budget_ledger ADD CONSTRAINT ck_budget_ledger_ledger_unit_present "
        "CHECK (length(btrim(unit)) > 0) NOT VALID"
    )
    op.create_check_constraint(
        op.f("ck_budget_ledger_ledger_generation_positive"),
        "budget_ledger",
        "lease_generation IS NULL OR lease_generation >= 1",
    )
    op.execute(
        "ALTER TABLE budget_ledger ADD CONSTRAINT fk_budget_ledger_work_item_campaign "
        "FOREIGN KEY (work_item_id,campaign_id) REFERENCES work_items(work_item_id,campaign_id) "
        "ON DELETE RESTRICT NOT VALID"
    )
    op.execute(
        "ALTER TABLE budget_ledger ADD CONSTRAINT fk_budget_ledger_job_work_item "
        "FOREIGN KEY (job_id,work_item_id) REFERENCES jobs(job_id,work_item_id) "
        "ON DELETE RESTRICT NOT VALID"
    )
    op.execute(
        "ALTER TABLE budget_ledger ADD CONSTRAINT fk_budget_ledger_attempt_execution "
        "FOREIGN KEY (attempt_id,work_item_id,job_id) "
        "REFERENCES attempts(attempt_id,work_item_id,job_id) ON DELETE RESTRICT NOT VALID"
    )
    op.execute(
        "ALTER TABLE budget_ledger ADD CONSTRAINT "
        "fk_budget_ledger_settlement_reservation_identity FOREIGN KEY "
        "(reservation_entry_id,campaign_id,work_item_id,job_id,lease_generation) "
        "REFERENCES budget_ledger(entry_id,campaign_id,work_item_id,job_id,lease_generation) "
        "ON DELETE RESTRICT NOT VALID"
    )
    op.create_check_constraint(
        op.f("ck_budget_ledger_ledger_entry_shape"),
        "budget_ledger",
        "(kind = 'reserved' AND reservation_entry_id IS NULL AND job_id IS NOT NULL "
        "AND lease_generation IS NOT NULL AND attempt_id IS NULL) OR "
        "(kind IN ('consumed','released','adjusted_up','adjusted_down') AND "
        "((reservation_entry_id IS NULL AND job_id IS NULL AND lease_generation IS NULL) OR "
        "(reservation_entry_id IS NOT NULL AND job_id IS NOT NULL "
        "AND lease_generation IS NOT NULL)))",
    )
    op.create_index(
        "uq_budget_ledger_reservation_execution",
        "budget_ledger",
        ["job_id", "lease_generation"],
        unique=True,
        postgresql_where=sa.text("kind = 'reserved'"),
    )
    op.create_index(
        "uq_budget_ledger_single_settlement",
        "budget_ledger",
        ["reservation_entry_id"],
        unique=True,
        postgresql_where=sa.text(
            "reservation_entry_id IS NOT NULL AND kind IN ('consumed','released')"
        ),
    )
    op.create_index(
        "uq_budget_ledger_single_actual_adjustment",
        "budget_ledger",
        ["reservation_entry_id"],
        unique=True,
        postgresql_where=sa.text(
            "reservation_entry_id IS NOT NULL AND kind IN ('adjusted_up','adjusted_down')"
        ),
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM budget_ledger WHERE kind IN "
        "('adjusted_up','adjusted_down')) THEN RAISE EXCEPTION "
        "'cannot downgrade while actual-cost adjustment ledger rows exist'; END IF; END $$"
    )
    op.drop_index("uq_budget_ledger_single_actual_adjustment", table_name="budget_ledger")
    op.drop_index("uq_budget_ledger_single_settlement", table_name="budget_ledger")
    op.drop_index("uq_budget_ledger_reservation_execution", table_name="budget_ledger")
    op.drop_constraint(op.f("ck_budget_ledger_ledger_entry_shape"), "budget_ledger", type_="check")
    op.drop_constraint(
        op.f("ck_budget_ledger_ledger_generation_positive"), "budget_ledger", type_="check"
    )
    op.drop_constraint(op.f("ck_budget_ledger_ledger_unit_present"), "budget_ledger", type_="check")
    op.drop_constraint(
        op.f("ck_budget_ledger_ledger_amount_positive"), "budget_ledger", type_="check"
    )
    op.drop_constraint(
        "fk_budget_ledger_settlement_reservation_identity",
        "budget_ledger",
        type_="foreignkey",
    )
    op.drop_constraint("fk_budget_ledger_attempt_execution", "budget_ledger", type_="foreignkey")
    op.drop_constraint("fk_budget_ledger_job_work_item", "budget_ledger", type_="foreignkey")
    op.drop_constraint("fk_budget_ledger_work_item_campaign", "budget_ledger", type_="foreignkey")
    op.drop_constraint("uq_budget_ledger_reservation_identity", "budget_ledger", type_="unique")
    op.drop_column("budget_ledger", "reservation_entry_id")
    op.drop_column("budget_ledger", "lease_generation")
    op.drop_column("budget_ledger", "attempt_id")
    op.drop_column("budget_ledger", "job_id")

    op.drop_constraint("uq_attempts_execution_identity", "attempts", type_="unique")
    op.drop_constraint("uq_jobs_work_item_identity", "jobs", type_="unique")
    op.drop_constraint("uq_work_items_campaign_identity", "work_items", type_="unique")
    op.drop_column("attempts", "adapter_started_at")

    op.drop_constraint(op.f("ck_campaigns_known_stopping_rule"), "campaigns", type_="check")
    op.drop_constraint(op.f("ck_campaigns_max_attempts_positive"), "campaigns", type_="check")
    op.drop_constraint(op.f("ck_campaigns_budget_unit_present"), "campaigns", type_="check")
    op.drop_constraint(
        op.f("ck_campaigns_attempt_estimate_within_hard_budget"), "campaigns", type_="check"
    )
    op.drop_constraint(
        op.f("ck_campaigns_per_attempt_estimate_positive"), "campaigns", type_="check"
    )
    op.drop_constraint(op.f("ck_campaigns_hard_budget_positive"), "campaigns", type_="check")
    op.drop_column("campaigns", "stopping_rule")
    op.drop_column("campaigns", "max_attempts")
    op.drop_column("campaigns", "budget_unit")
    op.drop_column("campaigns", "per_attempt_estimate")
    op.drop_column("campaigns", "hard_budget")
    op.drop_constraint(op.f("ck_budget_ledger_known_ledger_kind"), "budget_ledger", type_="check")
    op.create_check_constraint(
        op.f("ck_budget_ledger_known_ledger_kind"),
        "budget_ledger",
        "kind IN ('reserved','consumed','released')",
    )

"""Add the replay-complete campaign stream contract.

Revision ID: a93b7c1e4d62
Revises: 74e1b6a09d22
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a93b7c1e4d62"
down_revision: str | None = "74e1b6a09d22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_campaigns_known_event_stream_contract_version"),
        "campaigns",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_campaigns_known_event_stream_contract_version"),
        "campaigns",
        "event_stream_contract_version IN (0, 1, 2)",
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM campaigns WHERE "
        "event_stream_contract_version = 2) THEN RAISE EXCEPTION "
        "'cannot downgrade while replay-complete contract version 2 campaigns exist'; "
        "END IF; END $$"
    )
    op.drop_constraint(
        op.f("ck_campaigns_known_event_stream_contract_version"),
        "campaigns",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_campaigns_known_event_stream_contract_version"),
        "campaigns",
        "event_stream_contract_version IN (0, 1)",
    )

"""Add versioned, campaign-ordered event streams.

Existing campaigns are deliberately marked with contract version zero. Positions assigned to their
existing event rows are storage coordinates only; they do not manufacture facts that were never
recorded. New application code creates version-one campaigns and emits every required fact.

Revision ID: 8c4d7e2a91bf
Revises: 1e6a158aabea
Create Date: 2026-08-01 12:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8c4d7e2a91bf"
down_revision: str | None = "1e6a158aabea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column(
            "event_stream_contract_version", sa.Integer(), server_default="0", nullable=False
        ),
    )
    op.add_column(
        "campaigns",
        sa.Column(
            "event_stream_last_position", sa.BigInteger(), server_default="0", nullable=False
        ),
    )
    op.create_check_constraint(
        "known_event_stream_contract_version",
        "campaigns",
        "event_stream_contract_version IN (0, 1)",
    )
    op.create_check_constraint(
        "event_stream_position_non_negative",
        "campaigns",
        "event_stream_last_position >= 0",
    )

    op.add_column(
        "jobs", sa.Column("event_correlation_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("jobs", sa.Column("last_event_id", postgresql.UUID(as_uuid=True), nullable=True))

    op.add_column("events", sa.Column("campaign_position", sa.BigInteger(), nullable=True))
    op.execute(
        """
        WITH positioned AS (
            SELECT event_id,
                   row_number() OVER (
                       PARTITION BY campaign_id ORDER BY recorded_at, event_id
                   ) AS position
            FROM events
        )
        UPDATE events
        SET campaign_position = positioned.position
        FROM positioned
        WHERE events.event_id = positioned.event_id
        """
    )
    op.execute(
        """
        UPDATE campaigns
        SET event_stream_last_position = positions.last_position
        FROM (
            SELECT campaign_id, max(campaign_position) AS last_position
            FROM events
            GROUP BY campaign_id
        ) AS positions
        WHERE campaigns.campaign_id = positions.campaign_id
        """
    )
    op.alter_column("events", "campaign_position", nullable=False)
    op.drop_constraint("uq_events_aggregate_sequence", "events", type_="unique")
    op.create_unique_constraint(
        "uq_events_aggregate_sequence",
        "events",
        ["campaign_id", "aggregate_type", "aggregate_id", "sequence"],
    )
    op.create_unique_constraint(
        "uq_events_campaign_position", "events", ["campaign_id", "campaign_position"]
    )
    op.create_check_constraint(
        "campaign_position_starts_at_one", "events", "campaign_position >= 1"
    )
    op.drop_index("ix_events_replay", table_name="events")
    op.create_index(
        "ix_events_replay", "events", ["campaign_id", "campaign_position"], unique=False
    )


def downgrade() -> None:
    connection = op.get_bind()
    complete_campaigns = connection.execute(
        sa.text("SELECT count(*) FROM campaigns WHERE event_stream_contract_version > 0")
    ).scalar_one()
    if complete_campaigns:
        raise RuntimeError("cannot remove the event stream contract while complete campaigns exist")

    op.drop_index("ix_events_replay", table_name="events")
    op.create_index(
        "ix_events_replay",
        "events",
        ["campaign_id", "aggregate_id", "sequence"],
        unique=False,
    )
    op.drop_constraint(op.f("ck_events_campaign_position_starts_at_one"), "events", type_="check")
    op.drop_constraint("uq_events_campaign_position", "events", type_="unique")
    op.drop_constraint("uq_events_aggregate_sequence", "events", type_="unique")
    op.create_unique_constraint(
        "uq_events_aggregate_sequence", "events", ["aggregate_id", "sequence"]
    )
    op.drop_column("events", "campaign_position")
    op.drop_column("jobs", "last_event_id")
    op.drop_column("jobs", "event_correlation_id")
    op.drop_constraint(
        op.f("ck_campaigns_event_stream_position_non_negative"), "campaigns", type_="check"
    )
    op.drop_constraint(
        op.f("ck_campaigns_known_event_stream_contract_version"), "campaigns", type_="check"
    )
    op.drop_column("campaigns", "event_stream_last_position")
    op.drop_column("campaigns", "event_stream_contract_version")

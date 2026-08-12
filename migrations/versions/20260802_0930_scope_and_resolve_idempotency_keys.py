"""Make the idempotency record decide, and say what it produced.

Two changes, both so a database constraint rather than a prior read arbitrates a concurrent retry:

* the primary key becomes `(scope, idempotency_key)`, so a token chosen by a caller for one
  operation cannot answer a different operation with the first one's response;
* the campaign the key produced becomes a typed column with a deferred foreign key, so the
  reservation can be the first statement of the submission transaction and still be referentially
  checked at commit.

Existing rows keep their key and are backfilled from the response they already stored.

Revision ID: b3f18d4c07ae
Revises: 8c4d7e2a91bf
Create Date: 2026-08-02 09:30:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b3f18d4c07ae"
down_revision: str | None = "8c4d7e2a91bf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "idempotency_keys",
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # Existing records already name their campaign inside the stored response. Reading it out is a
    # move, not a derivation: the value is the one the endpoint returned to the caller.
    op.execute(
        """
        UPDATE idempotency_keys
        SET campaign_id = (response ->> 'campaign_id')::uuid
        WHERE response ? 'campaign_id'
          AND (response ->> 'campaign_id') ~
              '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
          AND EXISTS (
              SELECT 1 FROM campaigns
              WHERE campaigns.campaign_id = (idempotency_keys.response ->> 'campaign_id')::uuid
          )
        """
    )
    op.create_foreign_key(
        "fk_idempotency_keys_campaign_id_campaigns",
        "idempotency_keys",
        "campaigns",
        ["campaign_id"],
        ["campaign_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.drop_constraint("pk_idempotency_keys", "idempotency_keys", type_="primary")
    op.create_primary_key("pk_idempotency_keys", "idempotency_keys", ["scope", "idempotency_key"])


def downgrade() -> None:
    connection = op.get_bind()
    collisions = connection.execute(
        sa.text(
            """
            SELECT count(*) FROM (
                SELECT idempotency_key FROM idempotency_keys
                GROUP BY idempotency_key HAVING count(*) > 1
            ) AS duplicated
            """
        )
    ).scalar_one()
    if collisions:
        raise RuntimeError(
            "cannot narrow the idempotency primary key while one key is used in several scopes"
        )

    op.drop_constraint("pk_idempotency_keys", "idempotency_keys", type_="primary")
    op.create_primary_key("pk_idempotency_keys", "idempotency_keys", ["idempotency_key"])
    op.drop_constraint(
        "fk_idempotency_keys_campaign_id_campaigns", "idempotency_keys", type_="foreignkey"
    )
    op.drop_column("idempotency_keys", "campaign_id")

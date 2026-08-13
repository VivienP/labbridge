"""Merge the campaign and galvanostatic electrolysis migration histories.

Revision ID: d9c4e7a1b280
Revises: a93b7c1e4d62, 9a4e8f2c71d0
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "d9c4e7a1b280"
down_revision: tuple[str, str] = ("a93b7c1e4d62", "9a4e8f2c71d0")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the two additive histories without changing the schema."""


def downgrade() -> None:
    """Restore the two independent migration heads without changing the schema."""

"""add stream_url to tournament_players

Revision ID: f1bfdb00a655
Revises: cab631ee985c
Create Date: 2026-05-28 12:27:53.588766

Adds the optional `stream_url` column to `tournament_players`: a player's
official stream link for that tournament, set by an owner via the roster
PATCH endpoint and surfaced on the standings "Watch Live" column (#111).
Nullable — most rows carry no link, and it clears back to null.

Downgrade drops the column; values are lost.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1bfdb00a655"
down_revision: str | Sequence[str] | None = "cab631ee985c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "tournament_players",
        sa.Column("stream_url", sa.String(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("tournament_players", "stream_url")

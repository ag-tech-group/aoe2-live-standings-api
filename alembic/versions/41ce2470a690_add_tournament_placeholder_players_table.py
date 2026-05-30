"""add tournament_placeholder_players table

Revision ID: 41ce2470a690
Revises: abefba0d04b8
Create Date: 2026-05-29 21:41:25.270647

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "41ce2470a690"
down_revision: str | Sequence[str] | None = "abefba0d04b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "tournament_placeholder_players",
        sa.Column("tournament_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("presentation", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["tournament_id"], ["tournaments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tournament_id", "name"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("tournament_placeholder_players")

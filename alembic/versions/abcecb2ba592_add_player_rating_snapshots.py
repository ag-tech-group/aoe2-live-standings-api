"""add player_rating_snapshots

Revision ID: abcecb2ba592
Revises: 402ee822d8f4
Create Date: 2026-06-11 10:14:42.250375

Append-only observations of upstream's reported peak (``max_rating``), one
row per change per (profile, leaderboard). ``PlayerRating`` is overwritten
in place every poll, so the ranking metric had no recorded history and the
standings-history chart had to reconstruct past peaks from the match log —
provably wrong in both directions (#271). Expand-only: new table, no
existing readers/writers touched, safe during Cloud Run rollover.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "abcecb2ba592"
down_revision: str | Sequence[str] | None = "402ee822d8f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "player_rating_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("leaderboard_id", sa.Integer(), nullable=False),
        sa.Column("max_rating", sa.Integer(), nullable=False),
        sa.Column("current_rating", sa.Integer(), nullable=True),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["players.profile_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_player_rating_snapshots_profile_lb_observed",
        "player_rating_snapshots",
        ["profile_id", "leaderboard_id", "observed_at"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_player_rating_snapshots_profile_lb_observed",
        table_name="player_rating_snapshots",
    )
    op.drop_table("player_rating_snapshots")

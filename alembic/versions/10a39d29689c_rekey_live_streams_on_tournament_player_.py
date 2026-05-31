"""rekey live_streams on tournament_player_id

Revision ID: 10a39d29689c
Revises: 75c172c71c39
Create Date: 2026-05-31 10:43:29.061524

Re-keys ``live_streams`` from ``(profile_id, platform)`` to
``(tournament_player_id, platform)`` so placeholder roster rows
(``TournamentPlayer.profile_id IS NULL``) can be reported live too (#147).
The table is a transient snapshot the broadcast-live pollers fully rewrite
every cycle (~60s Twitch, ~30m YouTube), so we just drop and recreate —
the next tick rebuilds it. No backfill needed.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "10a39d29689c"
down_revision: str | Sequence[str] | None = "75c172c71c39"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_table("live_streams")
    op.create_table(
        "live_streams",
        sa.Column("tournament_player_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tournament_player_id"], ["tournament_players.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("tournament_player_id", "platform"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("live_streams")
    op.create_table(
        "live_streams",
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("profile_id", "platform"),
    )

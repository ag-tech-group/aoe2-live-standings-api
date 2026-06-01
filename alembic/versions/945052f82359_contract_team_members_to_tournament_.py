"""contract team_members to tournament_player_id key

Revision ID: 945052f82359
Revises: f812a0b45583
Create Date: 2026-06-01 12:53:59.155246

Contract step (#167) — finishes the expand-then-contract started in
``f812a0b45583``. Backfills any team_members row the previous revision
may have written without ``tournament_player_id`` during rollover,
makes the new column NOT NULL, swaps the PK from ``(team_id,
profile_id)`` to ``(team_id, tournament_player_id)``, and drops the
now-redundant ``profile_id`` column + its index.

Once this migration is in, ``team_members`` rows reference the
roster-row surrogate id directly, so a placeholder entrant (no
``profile_id``) can finally be assigned to a team — the original #167
ask.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "945052f82359"
down_revision: str | Sequence[str] | None = "f812a0b45583"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Defensive backfill: any row whose tournament_player_id is still
    # NULL (which can only happen if the previous Cloud Run revision
    # inserted during rollover, since the post-expand revision
    # dual-writes) gets backfilled the same way the expand migration
    # did. After this every row has tournament_player_id set, so
    # NOT NULL is safe.
    op.execute(
        """
        UPDATE team_members tm
           SET tournament_player_id = tp.id
          FROM teams t, tournament_players tp
         WHERE tm.team_id = t.id
           AND tp.tournament_id = t.tournament_id
           AND tp.profile_id    = tm.profile_id
           AND tm.tournament_player_id IS NULL
        """
    )

    op.alter_column("team_members", "tournament_player_id", nullable=False)

    # Swap the PK. The captain partial unique index is on ``team_id``
    # alone so the PK rotation doesn't disturb it.
    op.drop_constraint("team_members_pkey", "team_members", type_="primary")
    op.create_primary_key(
        "team_members_pkey",
        "team_members",
        ["team_id", "tournament_player_id"],
    )

    # profile_id and its index go away — every read after this lands
    # on the new key, which the previous revision never used.
    op.drop_index("ix_team_members_profile_id", table_name="team_members")
    op.drop_column("team_members", "profile_id")


def downgrade() -> None:
    """Downgrade schema."""
    # Re-add profile_id (nullable so we can backfill).
    op.add_column("team_members", sa.Column("profile_id", sa.Integer(), nullable=True))

    # Backfill from tournament_players via the new key. Placeholder
    # roster rows have no profile_id; their team memberships were only
    # writable post-contract, so they have no pre-contract equivalent
    # and are dropped here.
    op.execute(
        """
        UPDATE team_members tm
           SET profile_id = tp.profile_id
          FROM tournament_players tp
         WHERE tm.tournament_player_id = tp.id
           AND tp.profile_id IS NOT NULL
        """
    )
    op.execute("DELETE FROM team_members WHERE profile_id IS NULL")

    op.alter_column("team_members", "profile_id", nullable=False)
    op.create_index("ix_team_members_profile_id", "team_members", ["profile_id"])
    op.drop_constraint("team_members_pkey", "team_members", type_="primary")
    op.create_primary_key(
        "team_members_pkey",
        "team_members",
        ["team_id", "profile_id"],
    )

"""expand team_members with tournament_player_id

Revision ID: f812a0b45583
Revises: bd2d6eef00e8
Create Date: 2026-06-01 12:42:26.239549

Expand step (#167) for re-keying ``team_members`` from ``profile_id`` to
``tournament_player_id`` so placeholder roster entrants (no
``profile_id``) become teamable. Additive only — keeps the old PK,
column, and writes intact so the previous Cloud Run revision keeps
serving traffic during rollover.

Adds a nullable ``tournament_player_id`` column, backfills it from the
existing ``profile_id`` ↔ ``tournament_players.profile_id`` join
(scoped to each team's tournament), attaches an FK with CASCADE
delete, and adds an index. The contract step lands in a follow-up:
makes the column NOT NULL, swaps the PK to ``(team_id,
tournament_player_id)``, drops ``profile_id``, and switches the team
management endpoints over.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f812a0b45583"
down_revision: str | Sequence[str] | None = "bd2d6eef00e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add the new column as nullable so the previous revision (which
    # only writes ``profile_id``) can still INSERT during the Cloud Run
    # rollover. The contract migration tightens this to NOT NULL once
    # every writer populates it.
    op.add_column(
        "team_members",
        sa.Column("tournament_player_id", sa.Integer(), nullable=True),
    )

    # Backfill every existing row by joining ``tournament_players`` on
    # ``profile_id`` within the team's tournament. The CK on
    # tournament_players (profile_id XOR name) guarantees a unique match
    # for each polled team_member.
    op.execute(
        """
        UPDATE team_members tm
           SET tournament_player_id = tp.id
          FROM teams t, tournament_players tp
         WHERE tm.team_id = t.id
           AND tp.tournament_id = t.tournament_id
           AND tp.profile_id    = tm.profile_id
        """
    )

    # FK with CASCADE so deleting a roster row removes its team
    # memberships — same blast-radius as deleting a tournament today.
    op.create_foreign_key(
        "fk_team_members_tournament_player_id",
        "team_members",
        "tournament_players",
        ["tournament_player_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.create_index(
        "ix_team_members_tournament_player_id",
        "team_members",
        ["tournament_player_id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_team_members_tournament_player_id", table_name="team_members")
    op.drop_constraint("fk_team_members_tournament_player_id", "team_members", type_="foreignkey")
    op.drop_column("team_members", "tournament_player_id")

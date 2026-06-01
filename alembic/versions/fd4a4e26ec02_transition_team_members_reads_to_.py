"""transition team_members reads to tournament_player_id key

Revision ID: fd4a4e26ec02
Revises: f812a0b45583
Create Date: 2026-06-01 15:31:12.125147

Transition step (#167) — the middle of an expand -> transition -> contract
sequence. ``f812a0b45583`` (expand) added ``tournament_player_id`` and had
the writers dual-populate it. This step moves the *key* onto it: swaps the
PK from ``(team_id, profile_id)`` to ``(team_id, tournament_player_id)`` and
relaxes ``profile_id`` to NULLABLE — but deliberately KEEPS the ``profile_id``
column and its index.

Keeping ``profile_id`` is what makes the deploy zero-downtime: during the
Cloud Run rollover the previous revision still reads ``team_members.profile_id``
(e.g. the ``/standings`` team-join in ``_team_by_profile``), so the column
must survive until that revision has fully drained. The follow-up *contract*
migration drops it once no serving revision references it. Relaxing it to
NULLABLE lets the new revision — which keys on ``tournament_player_id`` and no
longer writes ``profile_id`` — insert placeholder team memberships (a roster
row with no minted ``profile_id``), the original #167 ask.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fd4a4e26ec02"
down_revision: str | Sequence[str] | None = "f812a0b45583"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Defensive backfill: any row whose tournament_player_id is still NULL
    # (only possible if the previous revision inserted during the expand
    # rollover, since the post-expand revision dual-writes) gets the same
    # backfill the expand migration did, so the NOT NULL below is safe.
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

    # Move the key onto the surrogate id. The captain partial unique index is
    # on ``team_id`` alone, so the PK rotation doesn't disturb it.
    op.drop_constraint("team_members_pkey", "team_members", type_="primary")
    op.create_primary_key(
        "team_members_pkey",
        "team_members",
        ["team_id", "tournament_player_id"],
    )

    # Relax profile_id to NULLABLE but KEEP the column + ix_team_members_profile_id.
    # The still-draining previous revision reads profile_id during rollover, so it
    # can't be dropped yet — the contract migration removes it next. NULLABLE also
    # lets placeholder memberships (no profile_id) be inserted.
    op.alter_column("team_members", "profile_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    # Placeholder memberships (profile_id NULL) only became writable after this
    # migration; they have no pre-transition equivalent, so drop them.
    op.execute("DELETE FROM team_members WHERE profile_id IS NULL")

    op.alter_column("team_members", "profile_id", existing_type=sa.Integer(), nullable=False)
    op.drop_constraint("team_members_pkey", "team_members", type_="primary")
    op.create_primary_key(
        "team_members_pkey",
        "team_members",
        ["team_id", "profile_id"],
    )
    # Revert tournament_player_id to the nullable state the expand step left it in.
    op.alter_column("team_members", "tournament_player_id", nullable=True)

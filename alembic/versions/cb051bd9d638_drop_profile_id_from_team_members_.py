"""drop profile_id from team_members (contract step)

Revision ID: cb051bd9d638
Revises: fd4a4e26ec02
Create Date: 2026-06-01 15:42:20.815140

Contract step (#167) — the final leg of expand -> transition -> contract.
The transition (``fd4a4e26ec02``) already moved the key to
``tournament_player_id`` and relaxed ``profile_id`` to NULLABLE, keeping the
column only so the previous revision could keep reading it through that
deploy's rollover. By the time this runs, the transition revision is the
minimum serving version and nothing reads ``team_members.profile_id`` any
more, so the column + its index can be dropped safely with zero downtime.

MUST deploy only after the transition (fd4a4e26ec02) is fully rolled out and
the pre-transition revision has drained — otherwise a still-serving old
revision queries a dropped column. (Enforced operationally, not in code.)
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cb051bd9d638"
down_revision: str | Sequence[str] | None = "fd4a4e26ec02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # No serving revision reads profile_id after the transition step, so the
    # now-redundant column and its index go away. The PK already moved to
    # (team_id, tournament_player_id) in fd4a4e26ec02.
    op.drop_index("ix_team_members_profile_id", table_name="team_members")
    op.drop_column("team_members", "profile_id")


def downgrade() -> None:
    """Downgrade schema."""
    # Re-add the column (nullable) and backfill from the roster row. We do NOT
    # restore the old (team_id, profile_id) PK or NOT NULL here — that's the
    # transition migration's downgrade to reverse; placeholder memberships have
    # no profile_id and must remain expressible.
    op.add_column("team_members", sa.Column("profile_id", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE team_members tm
           SET profile_id = tp.profile_id
          FROM tournament_players tp
         WHERE tm.tournament_player_id = tp.id
           AND tp.profile_id IS NOT NULL
        """
    )
    op.create_index("ix_team_members_profile_id", "team_members", ["profile_id"])

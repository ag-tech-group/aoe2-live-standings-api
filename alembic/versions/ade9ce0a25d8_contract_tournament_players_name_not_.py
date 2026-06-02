"""contract tournament_players: name NOT NULL (#187 phase 3)

Revision ID: ade9ce0a25d8
Revises: 1b4c7991f59a
Create Date: 2026-06-01 18:53:31.912641

Final leg of the #187 expand -> transition -> contract sequence. Phase 1
(expand, ``1b4c7991f59a``) dropped the XOR and backfilled ``name``; Phase 2
(transition) moved the serving code to always write ``name`` and address
rows by ``tournament_player_id``. By the time this runs, the Phase 2
revision is the minimum serving version, so every write sets ``name`` and
the column can be tightened to NOT NULL with zero downtime.

MUST deploy only after Phase 2 is fully rolled out and the Phase-1-era
revision (which could still insert a ``profile_id``-only row with a null
``name``) has drained. The defensive backfill below covers any such row
created during that window, mirroring the Phase 1 backfill, so the NOT NULL
is safe even if one slipped through.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ade9ce0a25d8"
down_revision: str | Sequence[str] | None = "1b4c7991f59a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Defensive backfill (same rule as Phase 1): a linked row added by the
    # pre-#187 code during the Phase-1->Phase-2 rollover could carry a null
    # name. Give it one before the NOT NULL so the tighten can't fail.
    op.execute(
        """
        UPDATE tournament_players tp
           SET name = COALESCE(p.alias, 'Player ' || tp.profile_id)
          FROM players p
         WHERE tp.profile_id = p.profile_id
           AND tp.name IS NULL
        """
    )
    op.execute(
        """
        UPDATE tournament_players
           SET name = 'Player ' || profile_id
         WHERE name IS NULL
           AND profile_id IS NOT NULL
        """
    )
    op.alter_column(
        "tournament_players",
        "name",
        existing_type=sa.String(length=64),
        nullable=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Re-allow null names. Rows keep their backfilled names; nothing to undo
    # on the data side.
    op.alter_column(
        "tournament_players",
        "name",
        existing_type=sa.String(length=64),
        nullable=True,
    )

"""add presentation bag to tournaments

Revision ID: 4582eb589b88
Revises: abcecb2ba592
Create Date: 2026-06-11 20:19:23.960422

Tournament-level mirror of the roster rows' opaque `presentation` bag
(54e868090062): owner-curated display data the API stores but never
interprets — phase schedule, bracket state, showmatch billing, whatever
the frontend defines. One bag means an event can transform for its
post-window phases (playoffs, grand finals) without tournament-format
concepts ever needing a migration or an API contract change.

Purely additive (server_default '{}'), so old Cloud Run revisions keep
serving through the deploy rollover.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4582eb589b88"
down_revision: str | Sequence[str] | None = "abcecb2ba592"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "tournaments",
        sa.Column("presentation", sa.JSON(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("tournaments", "presentation")

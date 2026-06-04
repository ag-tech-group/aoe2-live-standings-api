"""add title and category to live_streams

Revision ID: f5b83234e8df
Revises: bd121e55dd00
Create Date: 2026-06-03 19:19:15.951235

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f5b83234e8df"
down_revision: str | Sequence[str] | None = "bd121e55dd00"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Expand-only (#233): two nullable columns folded onto the live_streams
    snapshot rows the broadcast pollers rewrite each cycle. Adding a nullable
    column with no default is a metadata-only change in Postgres (no table
    rewrite, no lock contention), so it's safe to apply during a Cloud Run
    rollover — old revisions that don't write these columns keep working.
    """
    op.add_column("live_streams", sa.Column("title", sa.String(), nullable=True))
    op.add_column("live_streams", sa.Column("category", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("live_streams", "category")
    op.drop_column("live_streams", "title")

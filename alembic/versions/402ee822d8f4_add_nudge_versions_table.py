"""add nudge versions table

Revision ID: 402ee822d8f4
Revises: 6004c789de1b
Create Date: 2026-06-07 19:27:42.676883

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "402ee822d8f4"
down_revision: str | Sequence[str] | None = "6004c789de1b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Expand-only (#196 Option B): a tiny ``nudge_versions`` table that replaces
    Postgres LISTEN/NOTIFY for SSE nudges (LISTEN can't run through Managed
    Connection Pooling's transaction mode). The worker bumps a per-event
    ``polled_at`` on each commit; api instances poll it through their pooled
    engine and nudge their SSE clients when it advances. Creating a fresh table
    is metadata-only — safe during a Cloud Run rollover, and old revisions
    (still on LISTEN/NOTIFY) simply never read or write it.
    """
    op.create_table(
        "nudge_versions",
        sa.Column("event", sa.String(), nullable=False),
        sa.Column("polled_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("nudge_versions")

"""add profile aliases table

Revision ID: 6004c789de1b
Revises: f5b83234e8df
Create Date: 2026-06-06 13:04:58.906025

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6004c789de1b"
down_revision: str | Sequence[str] | None = "f5b83234e8df"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Expand-only (#349): a new ``profile_aliases`` name cache the recent-matches
    poller fills from each payload's ``profiles`` array — a ``profile_id`` →
    ``alias`` mirror for every participant we see, including untracked ladder
    opponents that never get a ``players`` row. Creating a fresh table is a
    metadata-only change (no rewrite of an existing table, no lock contention),
    so it's safe during a Cloud Run rollover — old revisions simply never read
    or write it.
    """
    op.create_table(
        "profile_aliases",
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("alias", sa.String(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("profile_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("profile_aliases")

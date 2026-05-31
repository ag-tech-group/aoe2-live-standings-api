"""add host stream urls and host live streams

Revision ID: 64de51a43607
Revises: 10a39d29689c
Create Date: 2026-05-31 11:39:56.145458

Backs host broadcast-live detection (#149): a per-tournament list of host
channel URLs the broadcast-live pollers resolve to liveness, and a
sibling-of-``live_streams`` snapshot table keyed on ``tournament_id``.

Both changes are additive and non-destructive — the previous Cloud Run
revision (which queries neither) keeps working through the rollover, so
this avoids the column-vanishes-mid-deploy race that bit #148.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "64de51a43607"
down_revision: str | Sequence[str] | None = "10a39d29689c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Default to an empty JSON array so existing tournaments report
    # `host_stream_live: false` without a backfill.
    op.add_column(
        "tournaments",
        sa.Column(
            "host_stream_urls",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )

    op.create_table(
        "host_live_streams",
        sa.Column("tournament_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["tournament_id"], ["tournaments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tournament_id", "platform"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("host_live_streams")
    op.drop_column("tournaments", "host_stream_urls")

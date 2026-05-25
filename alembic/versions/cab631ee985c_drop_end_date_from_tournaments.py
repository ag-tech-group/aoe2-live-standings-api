"""drop end_date from tournaments

Revision ID: cab631ee985c
Revises: c4e88b3a91d2
Create Date: 2026-05-24 21:34:57.558328

`end_date` was the original tournament-window upper bound. After #44
introduced `grand_finals_date`, the domain settled on
`grand_finals_date` carrying both roles — the hero-countdown target
*and* the terminating bound for tournament-scoped match filters.
`end_date` is now dead weight (#76); this migration drops the column.

Downgrade re-adds the column nullable; values are lost.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cab631ee985c"
down_revision: str | Sequence[str] | None = "c4e88b3a91d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("tournaments", "end_date")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        "tournaments",
        sa.Column("end_date", sa.DateTime(timezone=True), nullable=True),
    )

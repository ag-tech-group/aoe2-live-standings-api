"""add end_date alongside grand_finals_date

Revision ID: 61eb825d1551
Revises: 4582eb589b88
Create Date: 2026-06-11 20:23:51.605252

Expand phase of renaming `grand_finals_date` → `end_date` (#76 merged the
old `end_date` into a countdown-target field back when the two concepts
coincided; The King's Gauntlet's playoffs proved they don't — the data
window ends at the race end, days before the actual grand finals). A
straight column rename would 500 the previous Cloud Run revision through
the deploy rollover, so: add the new column, backfill, keep every write
path setting both, and drop the old column in a post-event contract
migration.

Purely additive + a same-table backfill — safe through the rollover.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "61eb825d1551"
down_revision: str | Sequence[str] | None = "4582eb589b88"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "tournaments",
        sa.Column("end_date", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE tournaments SET end_date = grand_finals_date")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("tournaments", "end_date")

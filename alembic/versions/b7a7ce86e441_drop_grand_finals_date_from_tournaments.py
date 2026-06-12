"""drop grand_finals_date from tournaments

Revision ID: b7a7ce86e441
Revises: 61eb825d1551
Create Date: 2026-06-12 10:50:41.860710

Contract phase of the grand_finals_date → end_date rename: the heal +
drop. MUST deploy strictly after the code cutover (the deploy that
stopped reading/writing this column) is fully serving — during THIS
deploy's rollover the previous revision is that column-free code, so
nothing queries the column when it drops. Folding the drop into the
code-cutover deploy would 500 the then-previous revision mid-rollover
(the 5xx class the expand→contract sequencing exists to avoid).

The heal covers the one desync the expand phase could leave behind: a
pre-rename revision writing during the 61eb825d1551 deploy rollover set
only grand_finals_date, leaving end_date NULL on that row. end_date is
authoritative everywhere else, so only NULLs are filled — a blanket
copy would clobber end_date edits made after the code cutover. Sanity-
check before merging: GET /v1/tournaments and confirm end_date is
populated correctly on every row.

Downgrade restores the column and backfills it from end_date.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7a7ce86e441"
down_revision: str | Sequence[str] | None = "61eb825d1551"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "UPDATE tournaments SET end_date = grand_finals_date "
        "WHERE end_date IS NULL AND grand_finals_date IS NOT NULL"
    )
    op.drop_column("tournaments", "grand_finals_date")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        "tournaments",
        sa.Column("grand_finals_date", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE tournaments SET grand_finals_date = end_date")

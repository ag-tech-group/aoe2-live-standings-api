"""add grand_finals_date to tournaments

Revision ID: f9e7f9f3561b
Revises: 8d202f1202b0
Create Date: 2026-05-23 00:01:53.798916

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f9e7f9f3561b"
down_revision: str | Sequence[str] | None = "8d202f1202b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "tournaments",
        sa.Column("grand_finals_date", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("tournaments", "grand_finals_date")

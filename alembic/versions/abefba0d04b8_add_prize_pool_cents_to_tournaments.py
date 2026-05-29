"""add prize_pool_cents to tournaments

Revision ID: abefba0d04b8
Revises: d172800e72a7
Create Date: 2026-05-29 16:17:42.515159

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "abefba0d04b8"
down_revision: str | Sequence[str] | None = "d172800e72a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "tournaments",
        sa.Column("prize_pool_cents", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("tournaments", "prize_pool_cents")

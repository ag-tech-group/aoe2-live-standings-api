"""add civilizations reference table

Revision ID: bd121e55dd00
Revises: ade9ce0a25d8
Create Date: 2026-06-02 23:56:47.483796

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bd121e55dd00"
down_revision: str | Sequence[str] | None = "ade9ce0a25d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "civilizations",
        sa.Column("civilization_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("civilization_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("civilizations")

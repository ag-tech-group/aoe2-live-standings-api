"""add rank_by to tournaments

Expand-only (#290): a NOT NULL column with a server_default, so every
pre-existing row (and any row inserted by a still-running old revision
during Cloud Run rollover) lands on 'peak_rating' — exactly the launch
behavior. No backfill, no contract break.

Revision ID: 7f07220ac5cb
Revises: a96e9a05b112
Create Date: 2026-07-25 15:12:07.073886

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7f07220ac5cb"
down_revision: str | Sequence[str] | None = "a96e9a05b112"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "tournaments",
        sa.Column(
            "rank_by",
            # Mirrors the model's non-native Enum storage (VARCHAR + CHECK):
            # the StrEnum *values* are stored, matching the JSON wire format.
            sa.Enum(
                "peak_rating",
                "current_rating",
                name="rank_by",
                native_enum=False,
            ),
            nullable=False,
            server_default="peak_rating",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("tournaments", "rank_by")

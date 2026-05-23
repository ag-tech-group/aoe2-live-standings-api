"""add idempotency_keys table

Revision ID: 1b245516564e
Revises: f9e7f9f3561b
Create Date: 2026-05-23 11:53:36.708186

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "1b245516564e"
down_revision: str | Sequence[str] | None = "f9e7f9f3561b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass

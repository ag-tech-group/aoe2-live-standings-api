"""add tournament_ids to nudge_versions

Expand-only (#293): a nullable JSON column carrying which tournaments a
nudge concerns. NULL means "unscoped — treat as all tournaments", which is
also what every pre-existing row and any row written by a still-running
old revision means, so mixed-revision rollover is safe and the SSE
contract stays backward compatible (clients that ignore the new payload
field behave exactly as before).

Revision ID: 62ef856f3e47
Revises: 7f07220ac5cb
Create Date: 2026-07-25 16:35:40.205688

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "62ef856f3e47"
down_revision: str | Sequence[str] | None = "7f07220ac5cb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("nudge_versions", sa.Column("tournament_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("nudge_versions", "tournament_ids")

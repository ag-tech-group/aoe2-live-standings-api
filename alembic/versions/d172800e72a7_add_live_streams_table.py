"""add live_streams table

Revision ID: d172800e72a7
Revises: 54e868090062
Create Date: 2026-05-29 15:40:26.204076

Backs broadcast-live detection (#112): a transient snapshot of which roster
profiles are streaming right now, partitioned by platform via a composite
(profile_id, platform) PK so the Twitch and YouTube pollers each replace
only their own rows. The standings endpoint folds presence here into the
`stream_live` flag. No data to backfill — the pollers populate it at runtime.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d172800e72a7"
down_revision: str | Sequence[str] | None = "54e868090062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "live_streams",
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("profile_id", "platform"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("live_streams")

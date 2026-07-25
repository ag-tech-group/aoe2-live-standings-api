"""add tournament_creators table

Expand-only (#296): a new allowlist table gating ``POST /v1/tournaments``.
No existing table is touched, so this is safe under Cloud Run revision
rollover — old revisions never see the table, new code requires a row
before allowing a create. Seeding operator rows is deployment data, not
schema: insert them out-of-band (same path the first ``tournament_owners``
rows used).

Revision ID: a96e9a05b112
Revises: b7a7ce86e441
Create Date: 2026-07-25 15:05:01.886107

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a96e9a05b112"
down_revision: str | Sequence[str] | None = "b7a7ce86e441"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "tournament_creators",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("tournament_creators")

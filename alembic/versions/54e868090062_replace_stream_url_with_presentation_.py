"""replace stream_url with presentation bag on tournament_players

Revision ID: 54e868090062
Revises: f1bfdb00a655
Create Date: 2026-05-28 16:26:20.844796

Generalizes the typed `stream_url` column (#111) into an opaque
`presentation` JSON bag: per-player, organizer-curated display data
(stream links, bio text, etc.) that the API stores but never interprets.
One bag means new editorial fields never need another migration.

`stream_url` carried no production data, so dropping it loses nothing.
Downgrade restores the nullable `stream_url` column; presentation data is
lost.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "54e868090062"
down_revision: str | Sequence[str] | None = "f1bfdb00a655"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "tournament_players",
        sa.Column("presentation", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.drop_column("tournament_players", "stream_url")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        "tournament_players",
        sa.Column("stream_url", sa.String(), nullable=True),
    )
    op.drop_column("tournament_players", "presentation")

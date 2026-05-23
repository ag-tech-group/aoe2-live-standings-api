"""ensure idempotency_keys table exists

Revision ID: c4e88b3a91d2
Revises: 1b245516564e
Create Date: 2026-05-23 13:30:00.000000

The original migration `1b245516564e` shipped with an empty body
(both `upgrade()` and `downgrade()` were `pass` stubs — a tool
mishap during authoring). Production's `alembic_version` was
advanced to that revision without the table being created, leaving
the schema permanently out of sync with the codebase.

The fix is forward-only: this revision idempotently creates the
table + index *if missing*. Works in three states:

  - Fresh DB (table absent, alembic at f9e7f9f3561b): the previous
    revision (now fixed in source) creates the table; this one is
    a no-op.
  - Fresh DB advanced past the broken revision (table absent,
    alembic at 1b245516564e): this revision creates the table.
  - Existing prod DB (table absent, alembic at 1b245516564e):
    same — this revision creates the table.

Uses `inspect()` rather than raw `CREATE TABLE IF NOT EXISTS` so
the migration stays portable across SQLAlchemy backends.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e88b3a91d2"
down_revision: str | Sequence[str] | None = "1b245516564e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema — create the table only if absent."""
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = inspector.get_table_names()

    if "idempotency_keys" not in existing_tables:
        op.create_table(
            "idempotency_keys",
            sa.Column("key", sa.String(length=64), nullable=False),
            sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("response_status", sa.Integer(), nullable=False),
            sa.Column("response_body", sa.LargeBinary(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("key"),
        )
        op.create_index(
            "ix_idempotency_keys_created_at",
            "idempotency_keys",
            ["created_at"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema — drop the table if present."""
    bind = op.get_bind()
    inspector = inspect(bind)
    if "idempotency_keys" in inspector.get_table_names():
        op.drop_index("ix_idempotency_keys_created_at", table_name="idempotency_keys")
        op.drop_table("idempotency_keys")

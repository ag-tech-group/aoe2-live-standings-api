"""add fan_allocations table and tournament fan_vote budgets (#209)

Revision ID: 94c456d362d6
Revises: ade9ce0a25d8
Create Date: 2026-06-02 13:01:06.474821

Community Hype voting data model (#209) — the first public-write surface
on this service. Two purely-additive changes, both zero-downtime, so this
can deploy while the pre-#209 revision is still serving:

1. **Two budget columns on ``tournaments``** (``fan_vote_budget_players``,
   ``fan_vote_budget_teams``), NOT NULL with a ``server_default`` of 100.
   In Postgres 11+ adding a NOT NULL column *with* a constant default is a
   metadata-only change — no table rewrite, no long lock — and it
   backfills every existing row to 100 atomically, so there's no
   expand→backfill→contract dance to stage here.

2. **The ``fan_allocations`` ballot table** — brand new, so nothing reads
   or writes it during rollover. ``coins >= 0`` is a CHECK; the unique key
   (tournament, voter, category, target) is what the PUT upserts against
   and keeps aggregate-on-read tallies drift-free; the tally index serves
   ``SUM(coins)`` / ``COUNT(DISTINCT voter_token)`` GROUP BY (category,
   target). ``category`` is a portable VARCHAR+CHECK (``native_enum=False``)
   so the same DDL runs on the SQLite test path.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "94c456d362d6"
down_revision: str | Sequence[str] | None = "ade9ce0a25d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Per-tournament Hype wallet sizes. server_default backfills existing
    #    rows to 100 (the launch event's budget) — metadata-only in PG 11+.
    op.add_column(
        "tournaments",
        sa.Column(
            "fan_vote_budget_players",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("100"),
        ),
    )
    op.add_column(
        "tournaments",
        sa.Column(
            "fan_vote_budget_teams",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("100"),
        ),
    )

    # 2. The ballot table.
    op.create_table(
        "fan_allocations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tournament_id", sa.Integer(), nullable=False),
        sa.Column("voter_token", sa.String(length=64), nullable=False),
        sa.Column(
            "category",
            sa.Enum("players", "teams", name="fan_vote_category", native_enum=False),
            nullable=False,
        ),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("coins", sa.Integer(), nullable=False),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("coins >= 0", name="ck_fan_allocations_coins_non_negative"),
        sa.ForeignKeyConstraint(["tournament_id"], ["tournaments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tournament_id",
            "voter_token",
            "category",
            "target_id",
            name="uq_fan_allocations_voter_target",
        ),
    )
    op.create_index(
        "ix_fan_allocations_tally",
        "fan_allocations",
        ["tournament_id", "category", "target_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_fan_allocations_tally", table_name="fan_allocations")
    op.drop_table("fan_allocations")
    op.drop_column("tournaments", "fan_vote_budget_teams")
    op.drop_column("tournaments", "fan_vote_budget_players")

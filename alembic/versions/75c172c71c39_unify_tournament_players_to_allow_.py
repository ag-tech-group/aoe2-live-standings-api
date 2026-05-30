"""unify tournament_players to allow placeholder rows

Revision ID: 75c172c71c39
Revises: 41ce2470a690
Create Date: 2026-05-29 22:25:48.208269

Collapses ``tournament_placeholder_players`` back into ``tournament_players``
so the host's "one roster" mental model maps to one row in one table. A
roster row carries EITHER a ``profile_id`` (a real polled identity) OR a
``name`` (an announced placeholder whose ``profile_id`` hasn't minted yet)
— XOR enforced at the schema level. A surrogate ``id`` PK lets promotion
from placeholder → real player happen via PATCH on a stable URL.

Data preserved: every existing ``tournament_players`` row gets a fresh
``id`` and ``name=NULL``; every ``tournament_placeholder_players`` row is
copied in with ``profile_id=NULL``. The placeholder table is then dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "75c172c71c39"
down_revision: str | Sequence[str] | None = "41ce2470a690"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # New columns. `id` starts nullable so we can backfill with ROW_NUMBER();
    # `name` is nullable forever (XOR with profile_id).
    op.add_column(
        "tournament_players",
        sa.Column("id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "tournament_players",
        sa.Column("name", sa.String(length=64), nullable=True),
    )

    # Backfill `id` deterministically for existing rows.
    op.execute(
        """
        WITH numbered AS (
            SELECT tournament_id, profile_id,
                   ROW_NUMBER() OVER (ORDER BY tournament_id, profile_id) AS new_id
              FROM tournament_players
        )
        UPDATE tournament_players tp
           SET id = numbered.new_id
          FROM numbered
         WHERE tp.tournament_id = numbered.tournament_id
           AND tp.profile_id   = numbered.profile_id
        """
    )

    # Swap the PK: drop the composite, lock `id`, attach a sequence so future
    # inserts auto-generate, then promote `id` to PK.
    op.drop_constraint("tournament_players_pkey", "tournament_players", type_="primary")
    op.alter_column("tournament_players", "id", nullable=False)
    op.execute("CREATE SEQUENCE tournament_players_id_seq")
    op.execute(
        """
        SELECT setval(
            'tournament_players_id_seq',
            COALESCE((SELECT MAX(id) FROM tournament_players), 1)
        )
        """
    )
    op.execute(
        "ALTER TABLE tournament_players "
        "ALTER COLUMN id SET DEFAULT nextval('tournament_players_id_seq')"
    )
    op.execute("ALTER SEQUENCE tournament_players_id_seq OWNED BY tournament_players.id")
    op.create_primary_key("tournament_players_pkey", "tournament_players", ["id"])

    # `profile_id` is now optional — placeholder rows carry NULL here.
    op.alter_column("tournament_players", "profile_id", nullable=True)

    # Pull every placeholder row into the unified table and drop the
    # parallel table. Identity transformation: same tournament_id +
    # presentation, profile_id is NULL, name carries through.
    op.execute(
        """
        INSERT INTO tournament_players (tournament_id, profile_id, name, presentation)
        SELECT tournament_id, NULL, name, presentation
          FROM tournament_placeholder_players
        """
    )
    op.drop_table("tournament_placeholder_players")

    # XOR: exactly one of (profile_id, name) is set on every row. Postgres
    # treats NULL = NULL as unknown, so we use `IS NULL`-pair comparisons.
    op.create_check_constraint(
        "ck_tournament_players_profile_id_xor_name",
        "tournament_players",
        "(profile_id IS NULL) <> (name IS NULL)",
    )

    # Unique within a tournament. Postgres treats NULLs as distinct in
    # UNIQUE by default, so multiple placeholder rows with NULL profile_id
    # coexist, and multiple polled rows with NULL name coexist — the XOR
    # check ensures NULL is paired with a non-null sibling on every row.
    op.create_unique_constraint(
        "uq_tournament_players_tournament_id_profile_id",
        "tournament_players",
        ["tournament_id", "profile_id"],
    )
    op.create_unique_constraint(
        "uq_tournament_players_tournament_id_name",
        "tournament_players",
        ["tournament_id", "name"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Restore the parallel placeholder table and move placeholder rows back.
    op.create_table(
        "tournament_placeholder_players",
        sa.Column("tournament_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("presentation", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["tournament_id"], ["tournaments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tournament_id", "name"),
    )
    op.execute(
        """
        INSERT INTO tournament_placeholder_players (tournament_id, name, presentation)
        SELECT tournament_id, name, presentation
          FROM tournament_players
         WHERE profile_id IS NULL
        """
    )
    op.execute("DELETE FROM tournament_players WHERE profile_id IS NULL")

    # Tear down the unified constraints + columns and restore the composite PK.
    op.drop_constraint(
        "uq_tournament_players_tournament_id_name", "tournament_players", type_="unique"
    )
    op.drop_constraint(
        "uq_tournament_players_tournament_id_profile_id",
        "tournament_players",
        type_="unique",
    )
    op.drop_constraint(
        "ck_tournament_players_profile_id_xor_name",
        "tournament_players",
        type_="check",
    )
    op.alter_column("tournament_players", "profile_id", nullable=False)
    op.drop_constraint("tournament_players_pkey", "tournament_players", type_="primary")
    op.create_primary_key(
        "tournament_players_pkey", "tournament_players", ["tournament_id", "profile_id"]
    )
    op.execute("ALTER TABLE tournament_players ALTER COLUMN id DROP DEFAULT")
    op.execute("DROP SEQUENCE tournament_players_id_seq")
    op.drop_column("tournament_players", "id")
    op.drop_column("tournament_players", "name")

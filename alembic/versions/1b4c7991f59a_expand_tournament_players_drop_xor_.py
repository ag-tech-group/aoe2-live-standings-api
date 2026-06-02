"""expand tournament_players: drop xor, backfill name (#187 phase 1)

Revision ID: 1b4c7991f59a
Revises: cb051bd9d638
Create Date: 2026-06-01 18:11:20.258831

Phase 1 (expand) of #187 — unify ``TournamentPlayer`` into one first-class
entity, dropping the placeholder-vs-polled two-class split. This step only
*loosens* the schema and *fills in* data; it is non-breaking to the live
code, so it can deploy while the pre-#187 revision is still serving.

Two changes, in this order (order matters):

1. **Drop the XOR check constraint** ``ck_tournament_players_profile_id_xor_name``
   (``(profile_id IS NULL) <> (name IS NULL)``). This must come first — the
   backfill below sets ``name`` on rows that already have a ``profile_id``,
   which the XOR would reject.

2. **Backfill ``name`` for every linked row** (``profile_id`` set) from the
   polled alias, so that after this migration every row has a ``name`` (the
   target model's always-present display label). Placeholder rows already
   have one and are left untouched (``name IS NULL`` guard). ``name`` stays
   NULLABLE here; the Phase 3 contract migration makes it NOT NULL once the
   new serving code (which always writes ``name``) is the minimum version.

Why this is safe for the still-running old revision: it enforces the XOR at
the *application* layer (``RosterPlayerCreate._exactly_one_identity``), so
new writes stay XOR-valid even though the DB no longer checks it; and its
reads take ``alias`` for linked rows / ``name`` for placeholders, simply
ignoring the new ``name`` we just wrote onto linked rows.

Live data at authoring time: one tournament (``kings-gauntlet``), 20 roster
rows, exactly one placeholder ("Jabo"), zero duplicate aliases — so the
backfill produces unique ``name`` values and won't trip
``uq_tournament_players_tournament_id_name``.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1b4c7991f59a"
down_revision: str | Sequence[str] | None = "cb051bd9d638"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Drop the XOR FIRST — backfilling name onto profile_id rows (step 2)
    #    would violate "(profile_id IS NULL) <> (name IS NULL)" otherwise.
    op.drop_constraint(
        "ck_tournament_players_profile_id_xor_name",
        "tournament_players",
        type_="check",
    )

    # 2a. Linked rows whose Player has been polled: take the polled alias.
    #     COALESCE guards a (DB-impossible but defensive) null alias with the
    #     same synthetic label step 2b uses.
    op.execute(
        """
        UPDATE tournament_players tp
           SET name = COALESCE(p.alias, 'Player ' || tp.profile_id)
          FROM players p
         WHERE tp.profile_id = p.profile_id
           AND tp.name IS NULL
        """
    )
    # 2b. Linked rows whose Player has not been polled yet (no players row to
    #     join, so step 2a skipped them): synthetic label keyed on profile_id,
    #     unique by construction. Replaced by the real alias on the next poll
    #     once the new serving code reads name as the display label.
    op.execute(
        """
        UPDATE tournament_players
           SET name = 'Player ' || profile_id
         WHERE profile_id IS NOT NULL
           AND name IS NULL
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Re-NULL the names we backfilled onto linked rows so the restored XOR
    # holds again. Placeholder rows (profile_id IS NULL) keep their name.
    # Any genuinely unified row a post-#187 revision may have written (both
    # profile_id and a meaningful name) loses its name here — unavoidable
    # when reverting to a schema that can't express it, mirroring the #167
    # contract downgrade dropping placeholder team memberships.
    op.execute("UPDATE tournament_players SET name = NULL WHERE profile_id IS NOT NULL")
    op.create_check_constraint(
        "ck_tournament_players_profile_id_xor_name",
        "tournament_players",
        "(profile_id IS NULL) <> (name IS NULL)",
    )

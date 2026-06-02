"""Community Hype voting — fan-allocation ballots (#209).

The first **public-write** surface on this service (everything else is
public-read / admin-write). Anonymous viewers spend a fixed Hype budget
across players and, separately, teams — a People's-Champion + Team-
Favorite layer for the event (#213).

Each row is one voter's ``coins`` on one target in one category: an
authoritative, re-editable **ballot** row, *not* a counter. Tallies are
aggregated on read (``SUM(coins)`` for total Hype, ``COUNT(DISTINCT
voter_token)`` for backers) so reallocation can never drift them — there
is no running total to keep in sync. The ``PUT`` endpoint (#210) replaces
a voter's ballot by upserting against the unique key below.
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class FanVoteCategory(StrEnum):
    """The two independent Hype wallets a voter spends from.

    A voter gets a separate budget per category (see
    ``Tournament.fan_vote_budget_*``); the two never share coins. Stored
    as a portable ``VARCHAR + CHECK`` (``native_enum=False``) so the same
    DDL runs under prod Postgres and the SQLite test path.
    """

    PLAYERS = "players"
    TEAMS = "teams"


class FanAllocation(Base):
    """One voter's Hype on one target, in one category — a single ballot row.

    Keyed on a **stable** ``target_id`` (#187-safe): a
    ``tournament_player_id`` when ``category`` is ``players``, a
    ``team_id`` when ``teams``. There is deliberately **no FK** on
    ``target_id`` — it's polymorphic across two tables by ``category``, so
    validity-for-this-tournament is enforced at the write layer (#210),
    mirroring the FK-free ``profile_id`` columns elsewhere. The
    ``tournament_id`` FK *does* cascade, so deleting a tournament clears
    its ballots.
    """

    __tablename__ = "fan_allocations"

    id: Mapped[int] = mapped_column(primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"),
    )
    # Client-generated convenience key for "edit my ballot" — NOT a
    # security identity. It's spoofable, so Sybil resistance rests on
    # Turnstile + the IP-hash throttle (#211), never on this. Typically a
    # random UUID the FE persists in localStorage.
    voter_token: Mapped[str] = mapped_column(String(64))
    category: Mapped[FanVoteCategory] = mapped_column(
        Enum(
            FanVoteCategory,
            name="fan_vote_category",
            native_enum=False,
            # Store the StrEnum value (lowercase, matches the JSON wire
            # form) rather than the member name, so the CHECK values and
            # the inserted value agree. Mirrors Match.state / .outcome.
            values_callable=lambda enum: [e.value for e in enum],
        ),
    )
    # STABLE target id (#187-safe): tournament_player_id for players,
    # team_id for teams. No FK (polymorphic, see class docstring).
    target_id: Mapped[int]
    # Hype spent on this target. Non-negative (DB CHECK below); the
    # endpoint additionally enforces that a voter's per-category sum stays
    # within the tournament's budget.
    coins: Mapped[int]
    # Salted hash of the real client IP (#176 plumbing), the only abuse
    # signal we keep — never the raw IP (PII-light, #211). Nullable: a row
    # may predate the IP-hash plumbing, or be written where no client IP
    # is resolvable.
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        # One row per (voter, target) within a category — the PUT upserts
        # against this, so a re-submitted ballot updates in place rather
        # than duplicating (idempotent, last-write-wins). The left prefix
        # (tournament_id, voter_token) also serves the /me ballot lookup.
        UniqueConstraint(
            "tournament_id",
            "voter_token",
            "category",
            "target_id",
            name="uq_fan_allocations_voter_target",
        ),
        # The tally GROUP BY: SUM(coins) + COUNT(DISTINCT voter_token) per
        # (category, target_id) within a tournament.
        Index(
            "ix_fan_allocations_tally",
            "tournament_id",
            "category",
            "target_id",
        ),
        # Non-negative coins, enforced server-side even against a direct
        # write; the endpoint validates per-category sums on top.
        CheckConstraint("coins >= 0", name="ck_fan_allocations_coins_non_negative"),
    )

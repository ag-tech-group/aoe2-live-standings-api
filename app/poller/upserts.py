"""Dialect-aware ``INSERT ... ON CONFLICT`` helpers for the polling worker.

The poller writes to four tables (``players``, ``player_ratings``, ``matches``,
``match_players``) with three semantic flavors:

- **Full overwrite on conflict** (``upsert_player``, ``upsert_player_rating``,
  ``upsert_match_from_recent``) — incoming row is authoritative.
- **Selective state update** (``upsert_match_from_live``) — the live poller
  only knows about ``state``; it must never roll back a row that the
  recent-matches poller has already marked ``completed`` (i.e. has a
  ``completed_at`` value set).
- **Insert-or-skip** (``upsert_match_player``) — per-player match outcomes
  are final once written; later polls of the same match are no-ops.

PostgreSQL is the production target; SQLite (3.24+) is used by the test
suite. Both speak the same ``ON CONFLICT`` SQL, but SQLAlchemy needs the
dialect-specific ``insert()`` factory — picked at runtime via
``dialect_insert``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Match, MatchPlayer, Player, PlayerRating


def dialect_insert(session: AsyncSession):
    """Return the dialect-appropriate ``insert()`` factory for this session.

    Both PostgreSQL and SQLite expose ``on_conflict_do_update`` / ``
    on_conflict_do_nothing`` with the same call signature; the imports
    just live in different submodules. This indirection keeps the rest of
    the module dialect-agnostic.
    """
    name = session.bind.dialect.name
    if name == "postgresql":
        return pg_insert
    if name == "sqlite":
        return sqlite_insert
    raise NotImplementedError(f"Upserts not implemented for dialect: {name}")


async def upsert_player(session: AsyncSession, data: dict[str, Any]) -> None:
    """Insert a Player, or overwrite all non-PK columns on profile_id conflict.

    ``updated_at`` is set to ``now()`` on both the insert and the conflict
    update path — the column's ``onupdate`` won't fire for ``ON CONFLICT``
    because that goes through raw SQL, not the ORM's update machinery.
    """
    insert = dialect_insert(session)
    values = {**data, "updated_at": func.now()}
    stmt = insert(Player).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["profile_id"],
        set_={k: getattr(stmt.excluded, k) for k in values if k != "profile_id"},
    )
    await session.execute(stmt)


async def upsert_player_rating(session: AsyncSession, data: dict[str, Any]) -> None:
    """Insert a PlayerRating, or overwrite on ``(profile_id, leaderboard_id)`` conflict."""
    insert = dialect_insert(session)
    values = {**data, "updated_at": func.now()}
    stmt = insert(PlayerRating).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["profile_id", "leaderboard_id"],
        set_={
            k: getattr(stmt.excluded, k)
            for k in values
            if k not in ("profile_id", "leaderboard_id")
        },
    )
    await session.execute(stmt)


async def upsert_match_from_recent(session: AsyncSession, data: dict[str, Any]) -> None:
    """Insert a Match (from ``getRecentMatchHistory``) or fully overwrite on conflict.

    The recent-matches feed carries the authoritative final state of a
    match (state, completed_at, description), so we let it overwrite
    anything the live poller wrote earlier.
    """
    insert = dialect_insert(session)
    values = {**data, "updated_at": func.now()}
    stmt = insert(Match).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["match_id"],
        set_={k: getattr(stmt.excluded, k) for k in values if k != "match_id"},
    )
    await session.execute(stmt)


async def upsert_match_from_live(session: AsyncSession, data: dict[str, Any]) -> None:
    """Insert a Match (from ``findAdvertisements``) or update its ``state`` on conflict.

    Lives on a narrower contract than ``upsert_match_from_recent``:

    - Only updates ``state`` (and ``updated_at``); leaves every other
      column alone.
    - Refuses to update if ``completed_at`` is already set, so the
      recent-matches feed's authoritative completion never gets
      clobbered by a stale live observation.
    """
    insert = dialect_insert(session)
    values = {**data, "updated_at": func.now()}
    stmt = insert(Match).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["match_id"],
        set_={
            "state": stmt.excluded.state,
            "updated_at": stmt.excluded.updated_at,
        },
        where=Match.completed_at.is_(None),
    )
    await session.execute(stmt)


async def upsert_match_player(session: AsyncSession, data: dict[str, Any]) -> None:
    """Insert a MatchPlayer or do nothing on ``(match_id, profile_id)`` conflict.

    Per-player outcomes are final once a match is written; later passes
    over the same match (e.g. recent-matches polls the same player again
    next cycle) shouldn't rewrite the row.
    """
    insert = dialect_insert(session)
    stmt = insert(MatchPlayer).values(**data)
    stmt = stmt.on_conflict_do_nothing(index_elements=["match_id", "profile_id"])
    await session.execute(stmt)

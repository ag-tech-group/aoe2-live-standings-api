"""Leaderboard endpoints: list metadata, and standings per leaderboard."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import leaderboards_cache
from app.database import get_async_session
from app.models import Player, PlayerRating
from app.schemas import LeaderboardRead, ListEnvelope, StandingRow, compute_last_polled_at

router = APIRouter(prefix="/leaderboards", tags=["leaderboards"])

# Standings update on the player-stats polling cadence (30s); 15s shared
# cache keeps worst-case staleness around 45s.
_LEADERBOARDS_CACHE_CONTROL = "public, max-age=15"


@router.get("")
async def list_leaderboards(response: Response) -> ListEnvelope[LeaderboardRead]:
    """Available leaderboards, sourced from the in-memory cache.

    The polling worker fills the cache at startup from upstream
    ``getAvailableLeaderboards`` and refreshes daily. Until that worker
    lands, this endpoint returns an empty list with ``last_polled_at: null``.
    """
    response.headers["Cache-Control"] = _LEADERBOARDS_CACHE_CONTROL
    return ListEnvelope[LeaderboardRead](
        last_polled_at=leaderboards_cache.get_last_refreshed_at(),
        items=list(leaderboards_cache.get_cache()),
    )


@router.get("/{leaderboard_id}/standings")
async def get_standings(
    leaderboard_id: int,
    response: Response,
    session: AsyncSession = Depends(get_async_session),
) -> ListEnvelope[StandingRow]:
    """Tracked players' ratings on one leaderboard, sorted by current rating desc.

    Joins ``PlayerRating`` with ``Player`` so each row contains both the
    rating numbers and the player identity (alias, country). The
    ``(leaderboard_id, current_rating)`` composite index on
    ``player_ratings`` covers this query.
    """
    response.headers["Cache-Control"] = _LEADERBOARDS_CACHE_CONTROL

    stmt = (
        select(Player, PlayerRating)
        .join(PlayerRating, PlayerRating.profile_id == Player.profile_id)
        .where(PlayerRating.leaderboard_id == leaderboard_id)
        .order_by(PlayerRating.current_rating.desc())
    )
    rows = (await session.execute(stmt)).all()

    items: list[StandingRow] = []
    timestamps: list[datetime | None] = []
    for player, rating in rows:
        items.append(
            StandingRow(
                profile_id=player.profile_id,
                alias=player.alias,
                country=player.country,
                current_rating=rating.current_rating,
                max_rating=rating.max_rating,
                wins=rating.wins,
                losses=rating.losses,
                streak=rating.streak,
                rank=rating.rank,
                rank_total=rating.rank_total,
                last_match_at=rating.last_match_at,
                updated_at=rating.updated_at,
            )
        )
        timestamps.append(player.updated_at)
        timestamps.append(rating.updated_at)

    return ListEnvelope[StandingRow](
        last_polled_at=compute_last_polled_at(timestamps),
        items=items,
    )

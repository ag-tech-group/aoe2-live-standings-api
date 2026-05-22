"""Leaderboard metadata endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Response

from app import leaderboards_cache
from app.schemas import LeaderboardRead, ListEnvelope

router = APIRouter(prefix="/leaderboards", tags=["leaderboards"])

# Leaderboard metadata changes rarely (only when Relic publishes a new
# ladder); a 15s shared cache is plenty.
_LEADERBOARDS_CACHE_CONTROL = "public, max-age=15"


@router.get("")
async def list_leaderboards(response: Response) -> ListEnvelope[LeaderboardRead]:
    """Available leaderboards, sourced from the in-memory cache.

    The polling worker fills the cache at startup from upstream
    ``getAvailableLeaderboards``. Each tournament tracks one of these by
    ``leaderboard_id``.
    """
    response.headers["Cache-Control"] = _LEADERBOARDS_CACHE_CONTROL
    return ListEnvelope[LeaderboardRead](
        last_polled_at=leaderboards_cache.get_last_refreshed_at(),
        items=list(leaderboards_cache.get_cache()),
    )

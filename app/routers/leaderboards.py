"""Leaderboard metadata endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_session
from app.models import Leaderboard
from app.schemas import LeaderboardRead, ListEnvelope, compute_last_polled_at

router = APIRouter(prefix="/leaderboards", tags=["leaderboards"])

# Leaderboard metadata changes rarely (only when Relic publishes a new
# ladder); a 15s shared cache is plenty. Browser revalidates every
# request (`max-age=0, must-revalidate`) — same pattern as the other
# live endpoints (#96); see app/routers/tournaments.py for the full
# rationale.
_LEADERBOARDS_CACHE_CONTROL = "public, s-maxage=15, max-age=0, must-revalidate"


@router.get("")
async def list_leaderboards(
    response: Response,
    session: AsyncSession = Depends(get_async_session),
) -> ListEnvelope[LeaderboardRead]:
    """Available leaderboards, sourced from the ``leaderboards`` table.

    The polling worker upserts rows here at startup from upstream
    ``getAvailableLeaderboards``. Each tournament tracks one of these by
    ``leaderboard_id``.
    """
    response.headers["Cache-Control"] = _LEADERBOARDS_CACHE_CONTROL
    stmt = select(Leaderboard).order_by(Leaderboard.leaderboard_id)
    rows = (await session.execute(stmt)).scalars().all()
    return ListEnvelope[LeaderboardRead](
        last_polled_at=compute_last_polled_at(r.updated_at for r in rows),
        items=[LeaderboardRead.model_validate(r) for r in rows],
    )

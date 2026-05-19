"""Live-feed endpoint: matches currently staging or in progress."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_async_session
from app.models import Match
from app.models.match import MatchState
from app.schemas import ListEnvelope, MatchRead, compute_last_polled_at

router = APIRouter(tags=["live"])

# The live-matches poller runs every 15s; 10s shared cache keeps tournament
# overlays close to real time while still smoothing out the burst from
# multiple viewers refreshing during the same poll cycle.
_LIVE_CACHE_CONTROL = "public, max-age=10"

_LIVE_STATES = (MatchState.STAGING, MatchState.IN_PROGRESS)


@router.get("/live")
async def get_live(
    response: Response,
    session: AsyncSession = Depends(get_async_session),
) -> ListEnvelope[MatchRead]:
    """Matches currently in ``staging`` or ``in_progress`` state.

    Backed by the ``ix_matches_state`` index. Ordered by ``started_at desc``
    so the most recent kick-offs sit at the top.
    """
    response.headers["Cache-Control"] = _LIVE_CACHE_CONTROL

    stmt = (
        select(Match)
        .where(Match.state.in_(_LIVE_STATES))
        .options(selectinload(Match.players))
        .order_by(Match.started_at.desc())
    )
    matches = (await session.execute(stmt)).scalars().all()

    items = [MatchRead.model_validate(m) for m in matches]
    timestamps: list[datetime | None] = [m.updated_at for m in matches]

    return ListEnvelope[MatchRead](
        last_polled_at=compute_last_polled_at(timestamps),
        items=items,
    )

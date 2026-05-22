"""Live-feed endpoint, scoped to a tournament: matches staging or in progress."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_async_session
from app.models import LiveMatchPlayer, Match, Tournament, TournamentPlayer
from app.models.match import MatchState
from app.routers.tournaments import get_tournament
from app.schemas import ListEnvelope, MatchRead, compute_last_polled_at

router = APIRouter(prefix="/tournaments/{tournament_slug}/live", tags=["live"])

# The live-matches poller runs every 15s; a 10s shared cache keeps
# tournament overlays close to real time while smoothing the burst from
# multiple viewers refreshing during the same poll cycle.
_LIVE_CACHE_CONTROL = "public, max-age=10"

_LIVE_STATES = (MatchState.STAGING, MatchState.IN_PROGRESS)


@router.get("")
async def get_live(
    response: Response,
    tournament: Tournament = Depends(get_tournament),
    session: AsyncSession = Depends(get_async_session),
) -> ListEnvelope[MatchRead]:
    """The tournament roster's matches currently in ``staging`` or ``in_progress``.

    Scoped via ``live_match_players`` — the live poller's per-cycle
    snapshot of who is in a live lobby. Ordered by ``started_at`` desc.
    """
    response.headers["Cache-Control"] = _LIVE_CACHE_CONTROL

    roster = select(TournamentPlayer.profile_id).where(
        TournamentPlayer.tournament_id == tournament.id
    )
    roster_live = select(LiveMatchPlayer.match_id).where(LiveMatchPlayer.profile_id.in_(roster))
    stmt = (
        select(Match)
        .where(Match.state.in_(_LIVE_STATES), Match.match_id.in_(roster_live))
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

"""Match endpoints: list and detail."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_async_session
from app.models import Match, MatchPlayer
from app.models.match import MatchState
from app.schemas import ListEnvelope, MatchDetail, MatchRead, compute_last_polled_at

router = APIRouter(prefix="/matches", tags=["matches"])

# List endpoint cache: completed and in-progress mixed; 15s is the same
# tier as players/standings.
_MATCHES_LIST_CACHE_CONTROL = "public, max-age=15"

# Completed match details rarely change (only `updated_at` ticks on a
# refresh poll), so a minute of shared cache is safe.
_COMPLETED_MATCH_CACHE_CONTROL = "public, max-age=60"

# In-progress matches change every poll cycle (15s for `/live`, 60s for
# `getRecentMatchHistory`); refuse to cache them anywhere.
_IN_PROGRESS_MATCH_CACHE_CONTROL = "no-store"


@router.get("")
async def list_matches(
    response: Response,
    session: AsyncSession = Depends(get_async_session),
    profile_id: int | None = Query(
        default=None,
        description="Restrict to matches the given profile_id appeared in.",
    ),
    leaderboard_id: int | None = Query(
        default=None,
        description="Restrict to matches on the given leaderboard.",
    ),
    state: MatchState | None = Query(
        default=None,
        description="Restrict to matches in this state.",
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Max matches to return (1-200, default 50).",
    ),
) -> ListEnvelope[MatchRead]:
    """Recent matches involving any tracked player, ordered by ``started_at`` desc.

    No pagination — the tournament-scale dataset comfortably fits inside a
    ``?limit`` window. Filters combine: ``?profile_id=N&state=completed``
    returns that player's recent completed matches.
    """
    response.headers["Cache-Control"] = _MATCHES_LIST_CACHE_CONTROL

    stmt = select(Match).options(selectinload(Match.players))
    if profile_id is not None:
        # Subquery rather than join+distinct so the limit applies to matches,
        # not (match, match_player) pairs.
        match_ids = select(MatchPlayer.match_id).where(MatchPlayer.profile_id == profile_id)
        stmt = stmt.where(Match.match_id.in_(match_ids))
    if leaderboard_id is not None:
        stmt = stmt.where(Match.leaderboard_id == leaderboard_id)
    if state is not None:
        stmt = stmt.where(Match.state == state)
    stmt = stmt.order_by(Match.started_at.desc()).limit(limit)

    matches = (await session.execute(stmt)).scalars().all()

    items = [MatchRead.model_validate(m) for m in matches]
    timestamps: list[datetime | None] = [m.updated_at for m in matches]

    return ListEnvelope[MatchRead](
        last_polled_at=compute_last_polled_at(timestamps),
        items=items,
    )


@router.get("/{match_id}")
async def get_match(
    match_id: int,
    response: Response,
    session: AsyncSession = Depends(get_async_session),
) -> MatchDetail:
    """A single match with all ``MatchPlayer`` rows.

    Cache headers are state-aware: ``no-store`` while the match is still
    in progress or staging (the response changes on every poll cycle);
    ``public, max-age=60`` once completed.
    """
    stmt = select(Match).where(Match.match_id == match_id).options(selectinload(Match.players))
    match = (await session.execute(stmt)).scalar_one_or_none()
    if match is None:
        raise HTTPException(status_code=404, detail="Match not found")

    response.headers["Cache-Control"] = (
        _COMPLETED_MATCH_CACHE_CONTROL
        if match.state == MatchState.COMPLETED
        else _IN_PROGRESS_MATCH_CACHE_CONTROL
    )

    detail = MatchDetail.model_validate(match)
    return detail.model_copy(update={"last_polled_at": match.updated_at})

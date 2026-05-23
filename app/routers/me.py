"""``/v1/me`` — per-user data for the authenticated caller.

A small umbrella surface for "things the frontend needs to know about
*this* user" — distinct from the global resource APIs. Lets the
frontend make one round-trip on app load to answer both "am I logged
in?" (401 vs 200) and "what can I admin?" (the owned-tournaments
list) instead of probing per-tournament endpoints.

All routes here are auth-required.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user_id
from app.database import get_async_session
from app.models import Tournament, TournamentOwner
from app.schemas.me import MeRead
from app.schemas.tournament import TournamentRead

router = APIRouter(prefix="/me", tags=["me"])


@router.get("")
async def get_me(
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_async_session),
) -> MeRead:
    """Identity + everything the caller can manage.

    Returns 401 when the request carries no valid token (the auth
    dependency raises). On 200, ``owned_tournaments`` is the list of
    tournaments — full ``TournamentRead`` objects — where the caller
    has an owner row, newest first. Empty list is the common case
    for a non-admin user.

    Cheap on prod scale (one indexed join), so the frontend can call
    on every page load. If that ever becomes a hot path, layer a
    short-TTL `Cache-Control: private` here.
    """
    stmt = (
        select(Tournament)
        .join(TournamentOwner, TournamentOwner.tournament_id == Tournament.id)
        .where(TournamentOwner.user_id == user_id)
        .order_by(Tournament.created_at.desc())
    )
    tournaments = (await session.execute(stmt)).scalars().all()
    return MeRead(
        user_id=user_id,
        owned_tournaments=[TournamentRead.model_validate(t) for t in tournaments],
    )

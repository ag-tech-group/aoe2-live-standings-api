"""``/v1/me`` — per-user data for the authenticated caller.

A small umbrella surface for "things the frontend needs to know about
*this* user" — distinct from the global resource APIs. Lets the
frontend make one round-trip on app load to answer both "am I logged
in?" (401 vs 200) and "what can I admin?" (the owned-tournaments
list) instead of probing per-tournament endpoints.

All routes here are auth-required.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user_id
from app.database import get_async_session
from app.models import Tournament, TournamentOwner
from app.schemas.me import MeRead
from app.schemas.tournament import TournamentRead

router = APIRouter(prefix="/me", tags=["me"])

# Per-user response — never cache anywhere. `private` keeps shared
# caches (CDN, corporate proxies) from storing one user's response and
# serving it to another (the response body is keyed to the JWT `sub`
# but the URL is not). `no-store` keeps the browser from holding it
# across a session — admin-grant + revoke mutations need to be
# reflected on the next page load with no hard-reload required.
#
# Without this, the global `cache_control_middleware` in app/main.py
# stamps `public, max-age=3600` (the catch-all default for 200 GETs),
# which is structurally wrong for per-user content. Fix lives at the
# endpoint until the middleware default is tightened separately.
_ME_CACHE_CONTROL = "private, no-store"


@router.get("")
async def get_me(
    response: Response,
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
    on every page load. See ``_ME_CACHE_CONTROL`` above for why we
    can't fall through to the middleware default here.
    """
    response.headers["Cache-Control"] = _ME_CACHE_CONTROL
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

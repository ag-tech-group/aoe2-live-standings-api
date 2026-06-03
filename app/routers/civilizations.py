"""Civilization reference endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_session
from app.models import Civilization
from app.schemas import CivilizationRead, ListEnvelope, compute_last_polled_at

router = APIRouter(prefix="/civilizations", tags=["civilizations"])

# Civilization metadata changes only when Relic ships a new civ — a 15s
# shared cache is plenty; the browser revalidates each request. Same
# pattern as /v1/leaderboards (see app/routers/leaderboards.py).
_CIVILIZATIONS_CACHE_CONTROL = "public, s-maxage=15, max-age=0, must-revalidate"


@router.get("")
async def list_civilizations(
    response: Response,
    session: AsyncSession = Depends(get_async_session),
) -> ListEnvelope[CivilizationRead]:
    """Civilizations, sourced from the ``civilizations`` table.

    The polling worker upserts rows here from the ``races`` array of upstream
    ``getAvailableLeaderboards``. Consumers map ``civilization_id`` (as
    returned by the civ-stats and recent-matchup endpoints) to a display name
    via this reference, so no one maintains a hand-built id→name list.
    """
    response.headers["Cache-Control"] = _CIVILIZATIONS_CACHE_CONTROL
    stmt = select(Civilization).order_by(Civilization.civilization_id)
    rows = (await session.execute(stmt)).scalars().all()
    return ListEnvelope[CivilizationRead](
        last_polled_at=compute_last_polled_at(r.updated_at for r in rows),
        items=[CivilizationRead.model_validate(r) for r in rows],
    )

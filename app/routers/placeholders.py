"""Placeholder roster endpoints — manage announced-but-unjoined entrants.

These are streamers on a host's announced roster whose ``profile_id``
hasn't minted yet (no first ranked match — there's no Steam→profile
lookup). They surface on ``/standings`` as a tail of rows with
``profile_id: null`` so the public sees the full announced field from
day one; the actual data lives in the ``tournament_placeholder_players``
table. See ``TournamentPlaceholderPlayer`` for the design rationale.

The lifecycle: the host POSTs a placeholder when announcing; when the
player mints a ``profile_id``, the host DELETEs the placeholder and
POSTs a real ``tournament_players`` row — promotion is two API ops
rather than one to keep this router orthogonal to the polled roster.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction, audit
from app.auth import get_current_user_id, require_tournament_owner
from app.cache import apply_live_cache_control
from app.database import get_async_session
from app.limiting import limiter
from app.models import Tournament, TournamentPlaceholderPlayer
from app.routers.tournaments import get_tournament
from app.schemas import (
    ListEnvelope,
    RosterPlaceholderCreate,
    RosterPlaceholderRead,
    RosterPlaceholderUpdate,
)

router = APIRouter(prefix="/tournaments/{tournament_slug}/placeholders", tags=["placeholders"])

# Placeholder state changes at organizer pace (manual edits), not on a
# polling cadence — but the read shape parallels the rest of the
# tournament's read surface, so reuse the live cache-control. Admin
# read-after-write skips it via the cookie-sensitive branch (#105).
_PLACEHOLDERS_CDN_SECONDS = 15


@router.get("")
async def list_placeholders(
    request: Request,
    response: Response,
    tournament: Tournament = Depends(get_tournament),
    session: AsyncSession = Depends(get_async_session),
) -> ListEnvelope[RosterPlaceholderRead]:
    """Every placeholder roster slot for this tournament, by name.

    Returned with the same ``ListEnvelope`` shape as ``/players`` for
    consumer parity, but ``last_polled_at`` is always null: placeholder
    rows aren't polled, so the freshness signal doesn't apply.
    """
    apply_live_cache_control(request, response, cdn_seconds=_PLACEHOLDERS_CDN_SECONDS)

    stmt = (
        select(TournamentPlaceholderPlayer)
        .where(TournamentPlaceholderPlayer.tournament_id == tournament.id)
        .order_by(TournamentPlaceholderPlayer.name.asc())
    )
    placeholders = (await session.execute(stmt)).scalars().all()
    return ListEnvelope[RosterPlaceholderRead](
        last_polled_at=None,
        items=[RosterPlaceholderRead.model_validate(p) for p in placeholders],
    )


@router.post("", status_code=204)
@limiter.limit("20/minute")
async def add_placeholder(
    request: Request,
    payload: RosterPlaceholderCreate,
    tournament: Tournament = Depends(require_tournament_owner),
    actor_user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    """Add a placeholder roster slot — owner-gated.

    409 if a placeholder with this name already exists on the tournament.
    The placeholder appears on the next ``/standings`` request; no
    polling involvement.
    """
    existing = (
        await session.execute(
            select(TournamentPlaceholderPlayer).where(
                TournamentPlaceholderPlayer.tournament_id == tournament.id,
                TournamentPlaceholderPlayer.name == payload.name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Placeholder already on the roster")

    session.add(
        TournamentPlaceholderPlayer(
            tournament_id=tournament.id,
            name=payload.name,
            presentation=payload.presentation,
        )
    )
    await session.commit()
    audit(
        AuditAction.PLACEHOLDER_ADD,
        actor_user_id=actor_user_id,
        tournament_slug=tournament.slug,
        tournament_id=tournament.id,
        target_placeholder_name=payload.name,
    )


@router.patch("/{name}", status_code=204)
@limiter.limit("20/minute")
async def update_placeholder(
    request: Request,
    name: str,
    payload: RosterPlaceholderUpdate,
    tournament: Tournament = Depends(require_tournament_owner),
    actor_user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    """Replace a placeholder's presentation bag — owner-gated.

    Same read-modify-write semantics as the real-player PATCH at
    ``/v1/tournaments/{slug}/players/{profile_id}``: the whole bag is
    replaced (callers carry unchanged keys forward). 404 if the named
    placeholder doesn't exist on this tournament.
    """
    entry = (
        await session.execute(
            select(TournamentPlaceholderPlayer).where(
                TournamentPlaceholderPlayer.tournament_id == tournament.id,
                TournamentPlaceholderPlayer.name == name,
            )
        )
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=404, detail="Placeholder not found in this tournament")

    entry.presentation = payload.presentation
    await session.commit()
    audit(
        AuditAction.PLACEHOLDER_UPDATE,
        actor_user_id=actor_user_id,
        tournament_slug=tournament.slug,
        tournament_id=tournament.id,
        target_placeholder_name=name,
        presentation_keys=sorted(payload.presentation),
    )


@router.delete("/{name}", status_code=204)
@limiter.limit("20/minute")
async def remove_placeholder(
    request: Request,
    name: str,
    tournament: Tournament = Depends(require_tournament_owner),
    actor_user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    """Remove a placeholder roster slot — owner-gated.

    404 if the placeholder doesn't exist. The usual promotion flow on
    placeholder→real-player ``profile_id`` mint is DELETE here followed
    by POST on ``/v1/tournaments/{slug}/players`` carrying the same
    presentation bag through.
    """
    entry = (
        await session.execute(
            select(TournamentPlaceholderPlayer).where(
                TournamentPlaceholderPlayer.tournament_id == tournament.id,
                TournamentPlaceholderPlayer.name == name,
            )
        )
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=404, detail="Placeholder not found in this tournament")

    await session.delete(entry)
    await session.commit()
    audit(
        AuditAction.PLACEHOLDER_REMOVE,
        actor_user_id=actor_user_id,
        tournament_slug=tournament.slug,
        tournament_id=tournament.id,
        target_placeholder_name=name,
    )

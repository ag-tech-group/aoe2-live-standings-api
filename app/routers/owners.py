"""Tournament-owner endpoints: list, grant, revoke. All owner-gated.

The CRUD surface over ``tournament_owners`` — the per-tournament
authorization layer #28 introduced. Until this router landed, rows in
that table had to be inserted via SQL; now a host can self-serve.

A criticalbit user's "ownership" of a tournament is a single row in
``tournament_owners``: present means owner, absent means not. There is
no role beyond binary; revocation is a hard delete (no audit trail
beyond Cloud Logging's request log). Revoking the *last* owner is a
422 — a tournament with zero owners would be uneditable.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction, audit
from app.auth import ACCESS_TOKEN_COOKIE, get_current_user_id, require_tournament_owner
from app.auth.users_client import fetch_identities
from app.database import get_async_session
from app.limiting import limiter
from app.models import Tournament, TournamentOwner
from app.schemas import TournamentOwnerCreate, TournamentOwnerRead

router = APIRouter(prefix="/tournaments/{tournament_slug}/owners", tags=["owners"])

# Admin-only, low-traffic, must reflect grant/revoke mutations on the
# very next request — same posture as the in-progress match detail at
# `_IN_PROGRESS_MATCH_CACHE_CONTROL` in matches.py. Without this, the
# global `cache_control_middleware` in app/main.py stamps
# `public, max-age=3600`, which pins both browser and CDN to a stale
# owners list for up to an hour after a revoke. Fix lives at the
# endpoint until the middleware default is tightened separately.
_OWNERS_LIST_CACHE_CONTROL = "no-store"


@router.get("")
async def list_tournament_owners(
    request: Request,
    response: Response,
    tournament: Tournament = Depends(require_tournament_owner),
    session: AsyncSession = Depends(get_async_session),
) -> list[TournamentOwnerRead]:
    """Every criticalbit user authorized to manage this tournament.

    Oldest first — useful when reasoning about who's been around longest
    and which slot is "the original creator". Owner-gated like the rest
    of this router.

    Each row is enriched with the user's ``display_name`` / ``email`` /
    ``avatar_url`` via a single batched call to auth-api's ``/users/lookup``
    (cached ~60s per id). The caller's access-token cookie is forwarded so
    the lookup runs as the same user that hit this endpoint. If auth-api
    is unreachable the enrichment fields fall back to ``null`` and the
    bare ``user_id`` / ``created_at`` rows still ship.
    """
    response.headers["Cache-Control"] = _OWNERS_LIST_CACHE_CONTROL
    stmt = (
        select(TournamentOwner)
        .where(TournamentOwner.tournament_id == tournament.id)
        .order_by(TournamentOwner.created_at)
    )
    rows = (await session.execute(stmt)).scalars().all()

    user_ids = [r.user_id for r in rows]
    access_token = request.cookies.get(ACCESS_TOKEN_COOKIE)
    identities = await fetch_identities(user_ids, access_token=access_token)

    result: list[TournamentOwnerRead] = []
    for row in rows:
        identity = identities.get(row.user_id)
        result.append(
            TournamentOwnerRead(
                user_id=row.user_id,
                created_at=row.created_at,
                display_name=identity.display_name if identity else None,
                email=identity.email if identity else None,
                avatar_url=identity.avatar_url if identity else None,
            )
        )
    return result


@router.post("", status_code=204)
@limiter.limit("5/minute")
async def grant_tournament_owner(
    request: Request,
    payload: TournamentOwnerCreate,
    tournament: Tournament = Depends(require_tournament_owner),
    actor_user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    """Grant ownership to a criticalbit user — owner-gated.

    409 if the user already owns this tournament. The pre-check makes
    the failure mode explicit at the API; the table's composite primary
    key on ``(tournament_id, user_id)`` is the safety net.
    """
    existing = (
        await session.execute(
            select(TournamentOwner.user_id).where(
                TournamentOwner.tournament_id == tournament.id,
                TournamentOwner.user_id == payload.user_id,
            )
        )
    ).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="User is already an owner")

    session.add(TournamentOwner(tournament_id=tournament.id, user_id=payload.user_id))
    await session.commit()
    audit(
        AuditAction.OWNER_GRANT,
        actor_user_id=actor_user_id,
        tournament_slug=tournament.slug,
        tournament_id=tournament.id,
        target_user_id=payload.user_id,
    )


@router.delete("/{user_id}", status_code=204)
@limiter.limit("5/minute")
async def revoke_tournament_owner(
    request: Request,
    user_id: str,
    tournament: Tournament = Depends(require_tournament_owner),
    actor_user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    """Revoke a user's ownership — owner-gated.

    404 if ``user_id`` is not an owner of this tournament. 422 if the
    revocation would leave the tournament with zero owners (it would
    then be uneditable). The last-owner guard is enforced in a single
    DELETE statement gated on a count subquery — atomic against the
    common case of sequential requests; a near-simultaneous double
    revoke against a two-owner tournament is recoverable but exceeds
    what a single statement can promise.
    """
    owner = (
        await session.execute(
            select(TournamentOwner.user_id).where(
                TournamentOwner.tournament_id == tournament.id,
                TournamentOwner.user_id == user_id,
            )
        )
    ).first()
    if owner is None:
        raise HTTPException(status_code=404, detail="User is not an owner")

    # Conditional DELETE: only fires when more than one owner remains.
    # The subquery evaluates inside the same statement as the delete, so
    # the count and the delete observe a consistent view.
    result = await session.execute(
        delete(TournamentOwner).where(
            TournamentOwner.tournament_id == tournament.id,
            TournamentOwner.user_id == user_id,
            select(func.count())
            .select_from(TournamentOwner)
            .where(TournamentOwner.tournament_id == tournament.id)
            .scalar_subquery()
            > 1,
        )
    )
    await session.commit()

    if result.rowcount == 0:
        # The owner row existed at the pre-check, so a rowcount of zero
        # here means the count guard rejected — they are the last owner.
        raise HTTPException(
            status_code=422,
            detail="Cannot revoke the last owner — the tournament would become uneditable",
        )

    audit(
        AuditAction.OWNER_REVOKE,
        actor_user_id=actor_user_id,
        tournament_slug=tournament.slug,
        tournament_id=tournament.id,
        target_user_id=user_id,
    )

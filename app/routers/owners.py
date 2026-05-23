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

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_tournament_owner
from app.database import get_async_session
from app.models import Tournament, TournamentOwner
from app.schemas import TournamentOwnerCreate, TournamentOwnerRead

router = APIRouter(prefix="/tournaments/{tournament_slug}/owners", tags=["owners"])


@router.get("")
async def list_tournament_owners(
    tournament: Tournament = Depends(require_tournament_owner),
    session: AsyncSession = Depends(get_async_session),
) -> list[TournamentOwnerRead]:
    """Every criticalbit user authorized to manage this tournament.

    Oldest first — useful when reasoning about who's been around longest
    and which slot is "the original creator". Owner-gated like the rest
    of this router.
    """
    stmt = (
        select(TournamentOwner)
        .where(TournamentOwner.tournament_id == tournament.id)
        .order_by(TournamentOwner.created_at)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [TournamentOwnerRead.model_validate(r) for r in rows]


@router.post("", status_code=204)
async def grant_tournament_owner(
    payload: TournamentOwnerCreate,
    tournament: Tournament = Depends(require_tournament_owner),
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


@router.delete("/{user_id}", status_code=204)
async def revoke_tournament_owner(
    user_id: str,
    tournament: Tournament = Depends(require_tournament_owner),
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

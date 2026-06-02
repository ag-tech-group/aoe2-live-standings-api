"""Player endpoints, scoped to a tournament: list, detail, and roster edits.

The roster is a single table (``tournament_players``); every row is one
first-class tournament player with a ``name`` and an optional ``profile_id``
link to a polled identity (#187). Rows are addressed by their surrogate
``tournament_player_id`` — list/detail/PATCH/DELETE all key on it — so an
unlinked entry (no ``profile_id`` yet) is just as addressable as a linked
one.

Setting ``profile_id`` via PATCH **links** an entry to a polled identity:
additive — the row's ``name`` is kept, ``profile_id`` is set, and the
``presentation`` bag carries through. The link is immutable once set.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.audit import AuditAction, audit
from app.auth import get_current_user_id, require_tournament_owner
from app.cache import apply_live_cache_control
from app.database import get_async_session
from app.limiting import limiter
from app.models import Match, MatchPlayer, Player, Tournament, TournamentPlayer
from app.routers.tournaments import get_tournament
from app.schemas import (
    ListEnvelope,
    MatchRead,
    PlayerDetail,
    PlayerRead,
    RosterPlayerCreate,
    RosterPlayerUpdate,
    compute_last_polled_at,
)

router = APIRouter(prefix="/tournaments/{tournament_slug}/players", tags=["players"])

# Polling cadence for player stats is 30s; CDN holds a shared copy for
# 15s so worst-case viewer staleness is ~45s. Admins reading right after
# a roster mutation get `private, no-store` instead — see app/cache.py
# for the full two-audience contract and #105 for the symptom that
# motivated the auth-aware split.
_PLAYERS_CDN_SECONDS = 15


def _unlinked_player_read(entry: TournamentPlayer) -> PlayerRead:
    """Build a PlayerRead for an unlinked row (no polled identity).

    Every polled field is null/empty; ``name`` is the display label and
    ``alias`` falls back to it so the consumer renders the row identically
    to a linked one (FE renders ``presentation.displayName ?? name``).
    """
    return PlayerRead(
        tournament_player_id=entry.id,
        profile_id=None,
        name=entry.name,
        alias=entry.name,
        country=None,
        steam_id=None,
        level=None,
        xp=None,
        region_id=None,
        clan_name=None,
        updated_at=None,
        presentation=entry.presentation,
        ratings=[],
    )


async def _roster_row_by_id(
    session: AsyncSession,
    tournament_id: int,
    tournament_player_id: int,
) -> TournamentPlayer | None:
    """Resolve a roster row by its surrogate id within the tournament."""
    stmt = select(TournamentPlayer).where(
        TournamentPlayer.tournament_id == tournament_id,
        TournamentPlayer.id == tournament_player_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


@router.get("")
async def list_players(
    request: Request,
    response: Response,
    tournament: Tournament = Depends(get_tournament),
    session: AsyncSession = Depends(get_async_session),
    leaderboard_id: int | None = Query(
        default=None,
        description="If set, each polled player's ratings are filtered to this leaderboard only.",
    ),
) -> ListEnvelope[PlayerRead]:
    """The tournament's roster — every entry, linked and unlinked interleaved.

    Sorted alphabetically by display name. An unlinked entry carries empty
    ``ratings`` and null polled fields; the ``leaderboard_id`` filter is a
    no-op on it. A linked entry whose poller hasn't fetched the ``Player``
    row yet (newly added, < one polling cycle old) is hidden until it has.
    """
    apply_live_cache_control(request, response, cdn_seconds=_PLAYERS_CDN_SECONDS)

    stmt = (
        select(TournamentPlayer, Player)
        .outerjoin(Player, TournamentPlayer.profile_id == Player.profile_id)
        .where(
            TournamentPlayer.tournament_id == tournament.id,
            or_(Player.profile_id.is_not(None), TournamentPlayer.profile_id.is_(None)),
        )
        .options(selectinload(Player.ratings))
        .order_by(TournamentPlayer.name)
    )
    rows = (await session.execute(stmt)).all()

    items: list[PlayerRead] = []
    timestamps: list[datetime | None] = []
    for entry, player in rows:
        if player is not None:
            # tournament_player_id and the display name live on the roster
            # row (TournamentPlayer), not the polled Player; stamp them on
            # the source so model_validate picks them up, mirroring how
            # presentation is folded in below.
            player.tournament_player_id = entry.id
            player.name = entry.name
            player_read = PlayerRead.model_validate(player)
            player_read.presentation = entry.presentation
            if leaderboard_id is not None:
                player_read.ratings = [
                    r for r in player_read.ratings if r.leaderboard_id == leaderboard_id
                ]
            timestamps.append(player_read.updated_at)
            timestamps.extend(r.updated_at for r in player_read.ratings)
        else:
            player_read = _unlinked_player_read(entry)
        items.append(player_read)

    return ListEnvelope[PlayerRead](
        last_polled_at=compute_last_polled_at(timestamps),
        items=items,
    )


@router.get("/{tournament_player_id}")
async def get_player(
    tournament_player_id: int,
    request: Request,
    response: Response,
    tournament: Tournament = Depends(get_tournament),
    session: AsyncSession = Depends(get_async_session),
    match_limit: int = Query(
        default=20,
        ge=1,
        le=100,
        description="Max recent matches to include (1-100, default 20).",
    ),
) -> PlayerDetail:
    """A roster player's profile — addressed by surrogate id (#187).

    A linked entry folds in its polled ``Player`` (ratings) and most recent
    matches. An unlinked entry — or a linked one the poller hasn't fetched
    yet — returns the same shape with empty polled enrichment (empty
    ``ratings`` / ``recent_matches``, null ``last_polled_at``). 404 if the
    id isn't on this tournament's roster.
    """
    apply_live_cache_control(request, response, cdn_seconds=_PLAYERS_CDN_SECONDS)

    roster_entry = await _roster_row_by_id(session, tournament.id, tournament_player_id)
    if roster_entry is None:
        raise HTTPException(status_code=404, detail="Player not found in this tournament")

    player = None
    if roster_entry.profile_id is not None:
        player = (
            await session.execute(
                select(Player)
                .where(Player.profile_id == roster_entry.profile_id)
                .options(selectinload(Player.ratings))
            )
        ).scalar_one_or_none()

    if player is None:
        # Unlinked, or linked but not yet polled — unified shape, no polled
        # enrichment. profile_id still surfaces when the entry is linked.
        base = _unlinked_player_read(roster_entry)
        base.profile_id = roster_entry.profile_id
        return PlayerDetail(**base.model_dump(), last_polled_at=None, recent_matches=[])

    matches_stmt = (
        select(Match)
        .join(MatchPlayer, MatchPlayer.match_id == Match.match_id)
        .where(MatchPlayer.profile_id == roster_entry.profile_id)
        .options(selectinload(Match.players))
        .order_by(Match.started_at.desc())
        .limit(match_limit)
    )
    matches = (await session.execute(matches_stmt)).scalars().all()
    recent_matches = [MatchRead.model_validate(m) for m in matches]

    timestamps: list[datetime | None] = [player.updated_at]
    timestamps.extend(r.updated_at for r in player.ratings)
    timestamps.extend(m.updated_at for m in matches)

    # tournament_player_id and the display name live on the roster row, not
    # the polled Player — stamp them so model_validate picks them up.
    player.tournament_player_id = roster_entry.id
    player.name = roster_entry.name
    detail = PlayerDetail.model_validate(player)
    return detail.model_copy(
        update={
            "last_polled_at": compute_last_polled_at(timestamps),
            "recent_matches": recent_matches,
            "presentation": roster_entry.presentation,
        }
    )


@router.post("", status_code=204)
@limiter.limit("20/minute")
async def add_roster_player(
    request: Request,
    payload: RosterPlayerCreate,
    tournament: Tournament = Depends(require_tournament_owner),
    actor_user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    """Add a roster entry — owner-gated.

    Body carries a required ``name`` (display label) and an optional
    ``profile_id`` linking it to a polled identity the poller will pick up
    next cycle. 409 if the ``name`` is already on the roster, or if the
    ``profile_id`` (when given) is.
    """
    if payload.profile_id is not None:
        profile_clash = (
            await session.execute(
                select(TournamentPlayer).where(
                    TournamentPlayer.tournament_id == tournament.id,
                    TournamentPlayer.profile_id == payload.profile_id,
                )
            )
        ).scalar_one_or_none()
        if profile_clash is not None:
            raise HTTPException(status_code=409, detail="Player already on the roster")

    name_clash = (
        await session.execute(
            select(TournamentPlayer).where(
                TournamentPlayer.tournament_id == tournament.id,
                TournamentPlayer.name == payload.name,
            )
        )
    ).scalar_one_or_none()
    if name_clash is not None:
        raise HTTPException(status_code=409, detail="Name already on the roster")

    session.add(
        TournamentPlayer(
            tournament_id=tournament.id,
            profile_id=payload.profile_id,
            name=payload.name,
            presentation=payload.presentation,
        )
    )
    await session.commit()
    audit(
        AuditAction.ROSTER_ADD,
        actor_user_id=actor_user_id,
        tournament_slug=tournament.slug,
        tournament_id=tournament.id,
        target_profile_id=payload.profile_id,
        target_placeholder_name=payload.name,
    )


@router.delete("/{tournament_player_id}", status_code=204)
@limiter.limit("20/minute")
async def remove_roster_player(
    request: Request,
    tournament_player_id: int,
    tournament: Tournament = Depends(require_tournament_owner),
    actor_user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    """Remove a roster entry by surrogate id — owner-gated.

    404 if no matching entry. The polled ``Player`` and rating rows are
    left untouched: the profile may still belong to another tournament's
    roster.
    """
    entry = await _roster_row_by_id(session, tournament.id, tournament_player_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Player not found in this tournament")

    target_profile_id = entry.profile_id
    target_name = entry.name
    await session.delete(entry)
    await session.commit()
    audit(
        AuditAction.ROSTER_REMOVE,
        actor_user_id=actor_user_id,
        tournament_slug=tournament.slug,
        tournament_id=tournament.id,
        target_profile_id=target_profile_id,
        target_placeholder_name=target_name,
    )


@router.patch("/{tournament_player_id}", status_code=204)
@limiter.limit("20/minute")
async def update_roster_player(
    request: Request,
    tournament_player_id: int,
    payload: RosterPlayerUpdate,
    tournament: Tournament = Depends(require_tournament_owner),
    actor_user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    """Edit a roster entry's presentation, or link it to a polled identity.

    Owner-gated; addressed by surrogate id. 404 if no matching entry.
    Body fields are both optional:
    - ``presentation``: replaces the whole bag (read-modify-write).
    - ``profile_id``: **links** an unlinked entry to a polled identity —
      additive, the row's ``name`` is kept. 422 if the entry is already
      linked (the link is immutable once set); 409 if the target
      ``profile_id`` is already on the roster.
    """
    entry = await _roster_row_by_id(session, tournament.id, tournament_player_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Player not found in this tournament")

    linked_to: int | None = None
    if payload.profile_id is not None:
        if entry.profile_id is not None:
            raise HTTPException(
                status_code=422,
                detail="profile_id is immutable once linked",
            )
        clash = (
            await session.execute(
                select(TournamentPlayer).where(
                    TournamentPlayer.tournament_id == tournament.id,
                    TournamentPlayer.profile_id == payload.profile_id,
                )
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Player {payload.profile_id} already on the roster",
            )
        linked_to = payload.profile_id
        # Additive link (#187): set the enrichment link, keep the name.
        entry.profile_id = payload.profile_id

    if payload.presentation is not None:
        entry.presentation = payload.presentation

    await session.commit()

    if linked_to is not None:
        # AuditAction.ROSTER_PROMOTE is a stable key (never renamed); it now
        # records an additive link rather than a name-clearing promotion.
        audit(
            AuditAction.ROSTER_PROMOTE,
            actor_user_id=actor_user_id,
            tournament_slug=tournament.slug,
            tournament_id=tournament.id,
            target_profile_id=linked_to,
            target_placeholder_name=entry.name,
            presentation_keys=(
                sorted(payload.presentation) if payload.presentation is not None else None
            ),
        )
    elif payload.presentation is not None:
        audit(
            AuditAction.ROSTER_UPDATE,
            actor_user_id=actor_user_id,
            tournament_slug=tournament.slug,
            tournament_id=tournament.id,
            target_profile_id=entry.profile_id,
            target_placeholder_name=entry.name,
            presentation_keys=sorted(payload.presentation),
        )

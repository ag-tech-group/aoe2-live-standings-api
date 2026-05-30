"""Player endpoints, scoped to a tournament: list, detail, and roster edits.

The roster is a single table (``tournament_players``) carrying two row
types under one schema: real polled identities (``profile_id`` set,
``name`` null) and announced placeholders (``profile_id`` null, ``name``
set — streamers whose ``profile_id`` hasn't minted yet). Routing handles
both with polymorphic URL dispatch on PATCH/DELETE — a numeric path
segment looks up by ``profile_id``; anything else looks up by ``name``.

Promotion (placeholder → real player) is a PATCH on the same URL: setting
``profile_id`` in the body atomically moves the row from the placeholder
state to the polled state, carrying the ``presentation`` bag through. URL
identity becomes the new ``profile_id`` after promotion.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, or_, select
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


def _placeholder_player_read(entry: TournamentPlayer) -> PlayerRead:
    """Build a PlayerRead for a placeholder row (no polled identity).

    Every polled field is null/empty; ``alias`` carries the host-given
    ``name`` so the consumer can render the row identically to a polled
    one (FE renders ``presentation.displayName ?? alias``).
    """
    return PlayerRead(
        profile_id=None,
        alias=entry.name or "",
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


async def _find_roster_entry(
    session: AsyncSession,
    tournament_id: int,
    lookup: str,
) -> TournamentPlayer | None:
    """Resolve a polymorphic URL lookup to a roster row, or None.

    Numeric lookup → ``profile_id`` match; non-numeric → ``name`` match.
    ``RosterPlayerCreate`` rejects all-digit names so the dispatch can't
    alias.
    """
    if lookup.isdigit():
        stmt = select(TournamentPlayer).where(
            TournamentPlayer.tournament_id == tournament_id,
            TournamentPlayer.profile_id == int(lookup),
        )
    else:
        stmt = select(TournamentPlayer).where(
            TournamentPlayer.tournament_id == tournament_id,
            TournamentPlayer.name == lookup,
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
    """The tournament's roster — polled identities and placeholders interleaved.

    Sorted alphabetically by the row's display name (``alias`` for polled
    rows, ``name`` for placeholders). Placeholders carry empty ``ratings``
    and null polled fields; the ``leaderboard_id`` filter is a no-op on
    them. Real entries whose poller hasn't fetched the ``Player`` row yet
    (newly added, < one polling cycle old) are hidden — same as before
    the unification.
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
        .order_by(func.coalesce(Player.alias, TournamentPlayer.name))
    )
    rows = (await session.execute(stmt)).all()

    items: list[PlayerRead] = []
    timestamps: list[datetime | None] = []
    for entry, player in rows:
        if player is not None:
            player_read = PlayerRead.model_validate(player)
            player_read.presentation = entry.presentation
            if leaderboard_id is not None:
                player_read.ratings = [
                    r for r in player_read.ratings if r.leaderboard_id == leaderboard_id
                ]
            timestamps.append(player_read.updated_at)
            timestamps.extend(r.updated_at for r in player_read.ratings)
        else:
            player_read = _placeholder_player_read(entry)
        items.append(player_read)

    return ListEnvelope[PlayerRead](
        last_polled_at=compute_last_polled_at(timestamps),
        items=items,
    )


@router.get("/{profile_id}")
async def get_player(
    profile_id: int,
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
    """A polled roster player's profile + ratings + most recent matches.

    Only addressable by ``profile_id`` (a positive integer in the path) —
    placeholder rows aren't shown here because there's no polled data to
    surface. 404 if the profile isn't on this tournament's roster or if
    it is, but as a placeholder. Matches are joined via
    ``MatchPlayer.profile_id`` (no FK back to ``Player``).
    """
    apply_live_cache_control(request, response, cdn_seconds=_PLAYERS_CDN_SECONDS)

    roster_entry = (
        await session.execute(
            select(TournamentPlayer).where(
                TournamentPlayer.tournament_id == tournament.id,
                TournamentPlayer.profile_id == profile_id,
            )
        )
    ).scalar_one_or_none()
    if roster_entry is None:
        raise HTTPException(status_code=404, detail="Player not found in this tournament")

    player_stmt = (
        select(Player).where(Player.profile_id == profile_id).options(selectinload(Player.ratings))
    )
    player = (await session.execute(player_stmt)).scalar_one_or_none()
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    matches_stmt = (
        select(Match)
        .join(MatchPlayer, MatchPlayer.match_id == Match.match_id)
        .where(MatchPlayer.profile_id == profile_id)
        .options(selectinload(Match.players))
        .order_by(Match.started_at.desc())
        .limit(match_limit)
    )
    matches = (await session.execute(matches_stmt)).scalars().all()
    recent_matches = [MatchRead.model_validate(m) for m in matches]

    timestamps: list[datetime | None] = [player.updated_at]
    timestamps.extend(r.updated_at for r in player.ratings)
    timestamps.extend(m.updated_at for m in matches)

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

    Body carries either ``profile_id`` (a polled identity that the
    poller will pick up next cycle) or ``name`` (an announced
    placeholder). 409 if the identifier is already on the roster.
    """
    if payload.profile_id is not None:
        existing_stmt = select(TournamentPlayer).where(
            TournamentPlayer.tournament_id == tournament.id,
            TournamentPlayer.profile_id == payload.profile_id,
        )
        conflict_detail = "Player already on the roster"
    else:
        existing_stmt = select(TournamentPlayer).where(
            TournamentPlayer.tournament_id == tournament.id,
            TournamentPlayer.name == payload.name,
        )
        conflict_detail = "Placeholder already on the roster"
    existing = (await session.execute(existing_stmt)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=conflict_detail)

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


@router.delete("/{lookup}", status_code=204)
@limiter.limit("20/minute")
async def remove_roster_player(
    request: Request,
    lookup: str,
    tournament: Tournament = Depends(require_tournament_owner),
    actor_user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    """Remove a roster entry — owner-gated.

    Polymorphic lookup: numeric path segment is a ``profile_id``,
    non-numeric is a placeholder ``name``. 404 if no matching entry.
    The polled ``Player`` and rating rows are left untouched: the
    profile may still belong to another tournament's roster.
    """
    entry = await _find_roster_entry(session, tournament.id, lookup)
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


@router.patch("/{lookup}", status_code=204)
@limiter.limit("20/minute")
async def update_roster_player(
    request: Request,
    lookup: str,
    payload: RosterPlayerUpdate,
    tournament: Tournament = Depends(require_tournament_owner),
    actor_user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    """Edit a roster entry's presentation, or promote a placeholder — owner-gated.

    Polymorphic lookup: numeric path segment is a ``profile_id``,
    non-numeric is a placeholder ``name``. 404 if no matching entry.

    Body fields are both optional:
    - ``presentation``: replaces the whole bag (read-modify-write).
    - ``profile_id``: only valid against a placeholder row, where it
      **promotes** the row to a polled identity in the same transaction
      (the ``name`` is cleared, ``profile_id`` is set; the
      ``presentation`` bag carries through unless the body also sets it).
      422 if the entry is already a polled identity (``profile_id`` is
      immutable on a real-player row); 409 if the target ``profile_id``
      is already on the roster.
    """
    entry = await _find_roster_entry(session, tournament.id, lookup)
    if entry is None:
        raise HTTPException(status_code=404, detail="Player not found in this tournament")

    promoted_to: int | None = None
    if payload.profile_id is not None:
        if entry.profile_id is not None:
            raise HTTPException(
                status_code=422,
                detail="profile_id is immutable on a polled-identity roster row",
            )
        existing = (
            await session.execute(
                select(TournamentPlayer).where(
                    TournamentPlayer.tournament_id == tournament.id,
                    TournamentPlayer.profile_id == payload.profile_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Player {payload.profile_id} already on the roster",
            )
        promoted_to = payload.profile_id
        entry.profile_id = payload.profile_id
        entry.name = None

    if payload.presentation is not None:
        entry.presentation = payload.presentation

    await session.commit()

    if promoted_to is not None:
        audit(
            AuditAction.ROSTER_PROMOTE,
            actor_user_id=actor_user_id,
            tournament_slug=tournament.slug,
            tournament_id=tournament.id,
            target_profile_id=promoted_to,
            target_placeholder_name=lookup if not lookup.isdigit() else None,
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

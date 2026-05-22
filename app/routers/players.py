"""Player endpoints, scoped to a tournament: list, detail, and roster edits."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import require_tournament_owner
from app.database import get_async_session
from app.models import Match, MatchPlayer, Player, Tournament, TournamentPlayer
from app.routers.tournaments import get_tournament
from app.schemas import (
    ListEnvelope,
    MatchRead,
    PlayerDetail,
    PlayerRead,
    RosterPlayerCreate,
    compute_last_polled_at,
)

router = APIRouter(prefix="/tournaments/{tournament_slug}/players", tags=["players"])

# Polling cadence for player stats is 30s; cache for 15s so a shared cache
# never serves data more than ~45s stale in the worst case.
_PLAYERS_CACHE_CONTROL = "public, max-age=15"


@router.get("")
async def list_players(
    response: Response,
    tournament: Tournament = Depends(get_tournament),
    session: AsyncSession = Depends(get_async_session),
    leaderboard_id: int | None = Query(
        default=None,
        description="If set, each player's ratings are filtered to this leaderboard only.",
    ),
) -> ListEnvelope[PlayerRead]:
    """The tournament's roster, with embedded ratings, alphabetical by alias.

    Players are returned regardless of whether they have a rating on the
    requested leaderboard (their ``ratings`` list may be empty). That keeps
    the response shape stable as a player's leaderboard participation
    changes.
    """
    response.headers["Cache-Control"] = _PLAYERS_CACHE_CONTROL

    roster = select(TournamentPlayer.profile_id).where(
        TournamentPlayer.tournament_id == tournament.id
    )
    stmt = (
        select(Player)
        .where(Player.profile_id.in_(roster))
        .options(selectinload(Player.ratings))
        .order_by(Player.alias)
    )
    players = (await session.execute(stmt)).scalars().all()

    items: list[PlayerRead] = []
    timestamps: list[datetime | None] = []
    for player in players:
        player_read = PlayerRead.model_validate(player)
        if leaderboard_id is not None:
            player_read.ratings = [
                r for r in player_read.ratings if r.leaderboard_id == leaderboard_id
            ]
        items.append(player_read)
        timestamps.append(player_read.updated_at)
        timestamps.extend(r.updated_at for r in player_read.ratings)

    return ListEnvelope[PlayerRead](
        last_polled_at=compute_last_polled_at(timestamps),
        items=items,
    )


@router.get("/{profile_id}")
async def get_player(
    profile_id: int,
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
    """A roster player's profile + ratings + most recent matches.

    404 if the profile isn't on this tournament's roster. Matches are
    joined via ``MatchPlayer.profile_id`` (no FK back to ``Player``).
    """
    response.headers["Cache-Control"] = _PLAYERS_CACHE_CONTROL

    in_roster = (
        await session.execute(
            select(TournamentPlayer.profile_id).where(
                TournamentPlayer.tournament_id == tournament.id,
                TournamentPlayer.profile_id == profile_id,
            )
        )
    ).first()
    if in_roster is None:
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
        }
    )


@router.post("", status_code=204)
async def add_roster_player(
    payload: RosterPlayerCreate,
    tournament: Tournament = Depends(require_tournament_owner),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    """Add a profile to the tournament's roster — owner-gated.

    409 if the profile is already on the roster. The polling worker picks
    the new profile up on its next cycle, so the edit takes effect without
    a redeploy.
    """
    existing = (
        await session.execute(
            select(TournamentPlayer).where(
                TournamentPlayer.tournament_id == tournament.id,
                TournamentPlayer.profile_id == payload.profile_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Player already on the roster")

    session.add(TournamentPlayer(tournament_id=tournament.id, profile_id=payload.profile_id))
    await session.commit()


@router.delete("/{profile_id}", status_code=204)
async def remove_roster_player(
    profile_id: int,
    tournament: Tournament = Depends(require_tournament_owner),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    """Remove a profile from the tournament's roster — owner-gated.

    404 if the profile isn't on the roster. The polled ``Player`` and
    rating rows are left untouched: the profile may still belong to
    another tournament's roster.
    """
    entry = (
        await session.execute(
            select(TournamentPlayer).where(
                TournamentPlayer.tournament_id == tournament.id,
                TournamentPlayer.profile_id == profile_id,
            )
        )
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=404, detail="Player not found in this tournament")

    await session.delete(entry)
    await session.commit()

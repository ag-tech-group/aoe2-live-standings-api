"""Tournament endpoints: list, detail, and per-tournament standings.

A tournament scopes the read surface — its roster (``TournamentPlayer``)
and its ``leaderboard_id`` select which players and ratings a standings
request sees. Matches and live state move under the same prefix in a
later stage.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_session
from app.models import (
    LiveMatchPlayer,
    Match,
    MatchOutcome,
    MatchPlayer,
    MatchState,
    Player,
    PlayerRating,
    Tournament,
    TournamentPlayer,
)
from app.schemas import ListEnvelope, StandingRow, TournamentRead, compute_last_polled_at

router = APIRouter(prefix="/tournaments", tags=["tournaments"])

# Standings update on the player-stats polling cadence (30s); 15s shared
# cache keeps worst-case staleness around 45s.
_STANDINGS_CACHE_CONTROL = "public, max-age=15"

# How many recent win/loss outcomes each standings row carries. Most-
# recent-first; the consumer renders a compact form strip and can show
# fewer client-side.
_RECENT_RESULTS_LIMIT = 10

# A player counts as "in a match" while their live match sits in one of
# these states — mirrors the live-feed filter.
_LIVE_MATCH_STATES = (MatchState.STAGING, MatchState.IN_PROGRESS)


async def get_tournament(
    tournament_slug: str,
    session: AsyncSession = Depends(get_async_session),
) -> Tournament:
    """Resolve the ``{tournament_slug}`` path parameter to a Tournament, or 404."""
    tournament = (
        await session.execute(select(Tournament).where(Tournament.slug == tournament_slug))
    ).scalar_one_or_none()
    if tournament is None:
        raise HTTPException(status_code=404, detail="Tournament not found")
    return tournament


@router.get("")
async def list_tournaments(
    session: AsyncSession = Depends(get_async_session),
) -> list[TournamentRead]:
    """Every tournament this deployment serves, newest first.

    Tournaments are configuration rather than polled data, so the response
    is a plain list — no ``last_polled_at`` envelope.
    """
    stmt = select(Tournament).order_by(Tournament.created_at.desc())
    tournaments = (await session.execute(stmt)).scalars().all()
    return [TournamentRead.model_validate(t) for t in tournaments]


@router.get("/{tournament_slug}")
async def get_tournament_detail(
    tournament: Tournament = Depends(get_tournament),
) -> TournamentRead:
    """A single tournament's metadata."""
    return TournamentRead.model_validate(tournament)


async def _recent_results_by_profile(
    session: AsyncSession,
    leaderboard_id: int,
    profile_ids: list[int],
) -> dict[int, list[MatchOutcome]]:
    """Map each profile to its recent win/loss outcomes on this leaderboard.

    One query over the whole standing set — completed matches on
    ``leaderboard_id``, newest first — bucketed per profile and capped at
    ``_RECENT_RESULTS_LIMIT``. In-progress matches carry a null ``outcome``
    and are filtered out. Tournament-scale match volume keeps this well
    short of needing a window function or a per-player query fan-out.
    """
    if not profile_ids:
        return {}

    stmt = (
        select(MatchPlayer.profile_id, MatchPlayer.outcome)
        .join(Match, Match.match_id == MatchPlayer.match_id)
        .where(
            Match.leaderboard_id == leaderboard_id,
            MatchPlayer.profile_id.in_(profile_ids),
            MatchPlayer.outcome.is_not(None),
        )
        .order_by(Match.started_at.desc())
    )

    results: dict[int, list[MatchOutcome]] = {}
    for profile_id, outcome in (await session.execute(stmt)).all():
        bucket = results.setdefault(profile_id, [])
        if len(bucket) < _RECENT_RESULTS_LIMIT:
            bucket.append(outcome)
    return results


async def _live_match_by_profile(
    session: AsyncSession,
    profile_ids: list[int],
) -> dict[int, int]:
    """Map each profile currently in a live match to that match's id.

    Reads the ``live_match_players`` snapshot the live poller fully
    rewrites every cycle, joined to ``matches`` to confirm the match is
    still in a live state — a just-finished match can briefly linger in an
    advertisement before the recent-matches feed flips it to ``completed``.
    Profiles absent from the result are not in a live match.
    """
    if not profile_ids:
        return {}

    stmt = (
        select(LiveMatchPlayer.profile_id, LiveMatchPlayer.match_id)
        .join(Match, Match.match_id == LiveMatchPlayer.match_id)
        .where(
            LiveMatchPlayer.profile_id.in_(profile_ids),
            Match.state.in_(_LIVE_MATCH_STATES),
        )
        .order_by(LiveMatchPlayer.match_id)
    )
    result = await session.execute(stmt)
    return dict(result.all())


@router.get("/{tournament_slug}/standings")
async def get_standings(
    response: Response,
    tournament: Tournament = Depends(get_tournament),
    session: AsyncSession = Depends(get_async_session),
) -> ListEnvelope[StandingRow]:
    """The tournament's players, ranked by current rating on its leaderboard.

    Scoped two ways: to the tournament's roster (``TournamentPlayer``) and
    to its ``leaderboard_id``. ``recent_results`` and live-match status are
    folded in by two further queries over the same standing set.
    """
    response.headers["Cache-Control"] = _STANDINGS_CACHE_CONTROL

    roster = select(TournamentPlayer.profile_id).where(
        TournamentPlayer.tournament_id == tournament.id
    )
    stmt = (
        select(Player, PlayerRating)
        .join(PlayerRating, PlayerRating.profile_id == Player.profile_id)
        .where(
            PlayerRating.leaderboard_id == tournament.leaderboard_id,
            Player.profile_id.in_(roster),
        )
        .order_by(PlayerRating.current_rating.desc())
    )
    rows = (await session.execute(stmt)).all()

    profile_ids = [player.profile_id for player, _ in rows]
    recent_results = await _recent_results_by_profile(
        session, tournament.leaderboard_id, profile_ids
    )
    live_match_ids = await _live_match_by_profile(session, profile_ids)

    items: list[StandingRow] = []
    timestamps: list[datetime | None] = []
    for player, rating in rows:
        items.append(
            StandingRow(
                profile_id=player.profile_id,
                alias=player.alias,
                country=player.country,
                current_rating=rating.current_rating,
                max_rating=rating.max_rating,
                wins=rating.wins,
                losses=rating.losses,
                streak=rating.streak,
                recent_results=recent_results.get(player.profile_id, []),
                rank=rating.rank,
                rank_total=rating.rank_total,
                in_match=player.profile_id in live_match_ids,
                live_match_id=live_match_ids.get(player.profile_id),
                last_match_at=rating.last_match_at,
                updated_at=rating.updated_at,
            )
        )
        timestamps.append(player.updated_at)
        timestamps.append(rating.updated_at)

    return ListEnvelope[StandingRow](
        last_polled_at=compute_last_polled_at(timestamps),
        items=items,
    )

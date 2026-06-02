"""Community Hype voting — the public-write fan-vote contract (#210).

The first public-write surface on this service. Anonymous viewers spend a
per-category Hype budget across players and teams; ballots are
authoritative, re-editable rows (#209) and tallies are aggregated on read
so they can't drift under reallocation.

Three routes under ``/v1/tournaments/{slug}/fan-votes``:

- ``PUT`` — replace the caller's entire ballot. Idempotent (last-write-
  wins), atomic across both categories, server-authoritative validation
  (per-category ``sum <= budget``, known targets only; ``coins >= 0`` and
  no duplicate targets come from the schema).
- ``GET /tallies`` — ``SUM(coins)`` + ``COUNT(DISTINCT voter_token)`` per
  target, both categories. Edge-cacheable.
- ``GET /me?voter_token=…`` — the caller's current ballot, for FE
  prefill. Never cached.

Turnstile siteverify, the IP-hash throttle, and the voting-window /
finals lock are the abuse layer (#211); the per-route IP rate limit below
is the interim backstop. This module lands the contract the FE
``generate-api`` consumes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import apply_live_cache_control
from app.database import get_async_session
from app.limiting import limiter
from app.models import FanAllocation, FanVoteCategory, Team, Tournament, TournamentPlayer
from app.routers.tournaments import get_tournament
from app.schemas import (
    FanVoteAllocation,
    FanVoteBallotRead,
    FanVoteBallotSubmit,
    FanVoteTallies,
    FanVoteTallyEntry,
)

router = APIRouter(prefix="/tournaments/{tournament_slug}/fan-votes", tags=["fan-votes"])

# Tally reads dwarf writes and are tiny (≤24 targets), so they coalesce at
# the CDN like the other viewer reads; a short TTL keeps the board lively.
# Aggregate-on-read stays correct under reallocation. #212 finalizes the
# Cloudflare edge rule and the optional ``votes`` SSE nudge.
_TALLIES_CDN_SECONDS = 10

# Per-IP write cap — generous for a human reallocating their wallet, a
# backstop against scripted spam until #211's Turnstile + salted-IP-hash
# throttle lands. The shared limiter keys on the real client IP (#176).
_FAN_VOTE_WRITE_LIMIT = "30/minute"


def _budget_for(tournament: Tournament, category: FanVoteCategory) -> int:
    """The voter's wallet size for one category on this tournament (#209)."""
    if category == FanVoteCategory.PLAYERS:
        return tournament.fan_vote_budget_players
    return tournament.fan_vote_budget_teams


async def _valid_target_ids(
    session: AsyncSession, tournament_id: int, category: FanVoteCategory
) -> set[int]:
    """The stable target ids a ballot may reference for this category.

    players → the tournament's roster row ids (``tournament_player_id``);
    teams → the tournament's team ids. Anything else is foreign/garbage and
    rejected by the caller.
    """
    if category == FanVoteCategory.PLAYERS:
        stmt = select(TournamentPlayer.id).where(TournamentPlayer.tournament_id == tournament_id)
    else:
        stmt = select(Team.id).where(Team.tournament_id == tournament_id)
    return set((await session.execute(stmt)).scalars().all())


async def _read_ballot(
    session: AsyncSession, tournament_id: int, voter_token: str
) -> FanVoteBallotRead:
    """The voter's current ballot, split by category (PUT echo + ``/me``)."""
    stmt = (
        select(FanAllocation.category, FanAllocation.target_id, FanAllocation.coins)
        .where(
            FanAllocation.tournament_id == tournament_id,
            FanAllocation.voter_token == voter_token,
        )
        .order_by(FanAllocation.category, FanAllocation.target_id)
    )
    rows = (await session.execute(stmt)).all()
    players = [
        FanVoteAllocation(target_id=target_id, coins=coins)
        for category, target_id, coins in rows
        if category == FanVoteCategory.PLAYERS
    ]
    teams = [
        FanVoteAllocation(target_id=target_id, coins=coins)
        for category, target_id, coins in rows
        if category == FanVoteCategory.TEAMS
    ]
    return FanVoteBallotRead(players=players, teams=teams)


@router.put("")
@limiter.limit(_FAN_VOTE_WRITE_LIMIT)
async def submit_ballot(
    request: Request,
    payload: FanVoteBallotSubmit,
    tournament: Tournament = Depends(get_tournament),
    session: AsyncSession = Depends(get_async_session),
) -> FanVoteBallotRead:
    """Replace the caller's entire Hype ballot — idempotent, atomic.

    Validation (server-authoritative, runs before any write so a bad
    category leaves the ballot untouched):

    - per-category ``sum(coins) <= budget`` → 422 over budget;
    - every ``target_id`` valid for this tournament + category → 422 on a
      foreign / unknown id;
    - ``coins >= 0`` and no duplicate targets — enforced by the schema.

    The replace clears the voter's rows for each category and re-inserts
    the submitted allocations, skipping ``coins == 0`` entries so a zeroed
    target leaves no row (keeps ``SUM`` / backer counts clean). One commit
    spans both categories, so the whole ballot swaps atomically and a
    retry / double-click is safe (last-write-wins). Returns the persisted
    ballot. 404 if the tournament slug is unknown.
    """
    categories = {
        FanVoteCategory.PLAYERS: payload.players,
        FanVoteCategory.TEAMS: payload.teams,
    }

    for category, allocations in categories.items():
        budget = _budget_for(tournament, category)
        total = sum(allocation.coins for allocation in allocations)
        if total > budget:
            raise HTTPException(
                status_code=422,
                detail=f"{category.value} ballot exceeds budget ({total} > {budget})",
            )
        if allocations:
            valid_ids = await _valid_target_ids(session, tournament.id, category)
            unknown = sorted({a.target_id for a in allocations} - valid_ids)
            if unknown:
                raise HTTPException(
                    status_code=422,
                    detail=f"unknown {category.value} target_id(s): {unknown}",
                )

    for category, allocations in categories.items():
        await session.execute(
            delete(FanAllocation).where(
                FanAllocation.tournament_id == tournament.id,
                FanAllocation.voter_token == payload.voter_token,
                FanAllocation.category == category,
            )
        )
        for allocation in allocations:
            if allocation.coins > 0:
                session.add(
                    FanAllocation(
                        tournament_id=tournament.id,
                        voter_token=payload.voter_token,
                        category=category,
                        target_id=allocation.target_id,
                        coins=allocation.coins,
                    )
                )
    await session.commit()

    return await _read_ballot(session, tournament.id, payload.voter_token)


@router.get("/tallies")
async def get_tallies(
    request: Request,
    response: Response,
    tournament: Tournament = Depends(get_tournament),
    session: AsyncSession = Depends(get_async_session),
) -> FanVoteTallies:
    """Aggregate Hype per target for both categories — drift-free, edge-cacheable.

    One grouped query: ``SUM(coins)`` (total Hype) and
    ``COUNT(DISTINCT voter_token)`` (backers) per ``(category, target_id)``.
    Each category list is ordered by coins descending (board order). Public
    viewer read — CDN-coalesced via the auth-aware cache helper.
    """
    apply_live_cache_control(request, response, cdn_seconds=_TALLIES_CDN_SECONDS)

    stmt = (
        select(
            FanAllocation.category,
            FanAllocation.target_id,
            func.sum(FanAllocation.coins).label("coins"),
            func.count(func.distinct(FanAllocation.voter_token)).label("backers"),
        )
        .where(FanAllocation.tournament_id == tournament.id)
        .group_by(FanAllocation.category, FanAllocation.target_id)
    )
    rows = (await session.execute(stmt)).all()

    players: list[FanVoteTallyEntry] = []
    teams: list[FanVoteTallyEntry] = []
    for category, target_id, coins, backers in rows:
        entry = FanVoteTallyEntry(target_id=target_id, coins=int(coins), backers=int(backers))
        (players if category == FanVoteCategory.PLAYERS else teams).append(entry)
    players.sort(key=lambda e: e.coins, reverse=True)
    teams.sort(key=lambda e: e.coins, reverse=True)

    return FanVoteTallies(players=players, teams=teams)


@router.get("/me")
async def get_my_ballot(
    response: Response,
    voter_token: str = Query(min_length=1, max_length=64),
    tournament: Tournament = Depends(get_tournament),
    session: AsyncSession = Depends(get_async_session),
) -> FanVoteBallotRead:
    """The caller's current ballot, keyed by ``voter_token`` — for FE prefill.

    Never cached: it's per-voter and keyed on a query param, so a shared
    cache must not hold it (``private, no-store``). An unknown token
    returns empty lists rather than 404 — a first-time voter has no ballot
    yet. 404 only if the tournament slug is unknown.
    """
    response.headers["Cache-Control"] = "private, no-store"
    return await _read_ballot(session, tournament.id, voter_token)

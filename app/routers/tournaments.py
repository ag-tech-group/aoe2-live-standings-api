"""Tournament endpoints: list, detail, per-tournament standings, and edits.

A tournament scopes the read surface — its roster (``TournamentPlayer``)
and its ``leaderboard_id`` select which players and ratings a standings
request sees. ``POST /`` is open to any authenticated criticalbit user
(the caller is recorded as the first owner); ``PATCH`` and
``DELETE /{slug}`` are owner-gated; every read route is public.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction, audit
from app.auth import get_current_user_id, require_tournament_owner
from app.database import get_async_session
from app.limiting import limiter
from app.models import (
    LiveMatchPlayer,
    Match,
    MatchOutcome,
    MatchPlayer,
    MatchState,
    Player,
    PlayerRating,
    Team,
    TeamMember,
    Tournament,
    TournamentOwner,
    TournamentPlayer,
)
from app.schemas import (
    ListEnvelope,
    StandingRow,
    TeamMemberRead,
    TeamStandingRow,
    TournamentCreate,
    TournamentRead,
    TournamentRecord,
    TournamentUpdate,
    compute_last_polled_at,
)

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


@router.post("", status_code=201)
@limiter.limit("5/minute")
async def create_tournament(
    request: Request,
    payload: TournamentCreate,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_async_session),
) -> TournamentRead:
    """Create a tournament — any authenticated criticalbit user may.

    The caller is recorded as the first owner, immediately able to ``PATCH``
    metadata, manage the roster + teams, and ``DELETE`` the tournament.
    409 if the slug is taken (it is unique across the deployment and how
    consumer URLs route to the right tournament). A competition window
    whose start falls after its grand finals is rejected with 422.
    """
    if (
        payload.start_date is not None
        and payload.grand_finals_date is not None
        and payload.start_date > payload.grand_finals_date
    ):
        raise HTTPException(
            status_code=422,
            detail="start_date must not be after grand_finals_date",
        )

    existing = (
        await session.execute(select(Tournament.id).where(Tournament.slug == payload.slug))
    ).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Tournament '{payload.slug}' already exists")

    tournament = Tournament(
        slug=payload.slug,
        name=payload.name,
        leaderboard_id=payload.leaderboard_id,
        start_date=payload.start_date,
        grand_finals_date=payload.grand_finals_date,
    )
    tournament.owners = [TournamentOwner(user_id=user_id)]
    session.add(tournament)
    await session.commit()
    audit(
        AuditAction.TOURNAMENT_CREATE,
        actor_user_id=user_id,
        tournament_slug=tournament.slug,
        tournament_id=tournament.id,
    )
    return TournamentRead.model_validate(tournament)


@router.get("/{tournament_slug}")
async def get_tournament_detail(
    tournament: Tournament = Depends(get_tournament),
) -> TournamentRead:
    """A single tournament's metadata."""
    return TournamentRead.model_validate(tournament)


@router.patch("/{tournament_slug}")
@limiter.limit("20/minute")
async def update_tournament(
    request: Request,
    payload: TournamentUpdate,
    tournament: Tournament = Depends(require_tournament_owner),
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_async_session),
) -> TournamentRead:
    """Edit a tournament's metadata — owner-gated.

    PATCH semantics: only the fields present in the request body change.
    ``start_date`` / ``grand_finals_date`` accept ``null`` to clear a
    bound; a competition window whose start falls after its grand finals
    is rejected with 422. ``slug`` is immutable — it is the key consumer
    URLs are built on.
    """
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(tournament, field, value)

    if (
        tournament.start_date is not None
        and tournament.grand_finals_date is not None
        and tournament.start_date > tournament.grand_finals_date
    ):
        raise HTTPException(
            status_code=422,
            detail="start_date must not be after grand_finals_date",
        )

    await session.commit()
    audit(
        AuditAction.TOURNAMENT_UPDATE,
        actor_user_id=user_id,
        tournament_slug=tournament.slug,
        tournament_id=tournament.id,
        changes=changes,
    )
    return TournamentRead.model_validate(tournament)


@router.delete("/{tournament_slug}", status_code=204)
@limiter.limit("3/minute")
async def delete_tournament(
    request: Request,
    tournament: Tournament = Depends(require_tournament_owner),
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    """Delete a tournament and everything tournament-scoped — owner-gated.

    Cascades to the roster (``tournament_players``), teams + team members
    (``teams`` / ``team_members``), and owners (``tournament_owners``)
    via the FKs' ``ON DELETE CASCADE``. Match history is not tournament-
    scoped and is preserved.
    """
    # Capture identifying fields before the delete — the audit log
    # outlives the row, so the slug + id need to be in the event.
    audit_slug = tournament.slug
    audit_id = tournament.id
    await session.delete(tournament)
    await session.commit()
    audit(
        AuditAction.TOURNAMENT_DELETE,
        actor_user_id=user_id,
        tournament_slug=audit_slug,
        tournament_id=audit_id,
    )


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


async def _tournament_record_by_profile(
    session: AsyncSession,
    tournament: Tournament,
    profile_ids: list[int],
) -> dict[int, TournamentRecord]:
    """Map each profile to its win/loss record within the tournament window.

    Counts completed matches on the tournament's leaderboard whose
    ``started_at`` falls inside ``[start_date, grand_finals_date]`` —
    a null bound is treated as open. Every profile gets an entry;
    those with no matches in the window get a zero record.
    """
    records = {
        profile_id: TournamentRecord(games_played=0, wins=0, losses=0, streak=0)
        for profile_id in profile_ids
    }
    if not profile_ids:
        return records

    stmt = (
        select(MatchPlayer.profile_id, MatchPlayer.outcome)
        .join(Match, Match.match_id == MatchPlayer.match_id)
        .where(
            Match.leaderboard_id == tournament.leaderboard_id,
            MatchPlayer.profile_id.in_(profile_ids),
            MatchPlayer.outcome.is_not(None),
        )
        .order_by(Match.started_at.desc())
    )
    if tournament.start_date is not None:
        stmt = stmt.where(Match.started_at >= tournament.start_date)
    if tournament.grand_finals_date is not None:
        stmt = stmt.where(Match.started_at <= tournament.grand_finals_date)

    outcomes: dict[int, list[MatchOutcome]] = {}
    for profile_id, outcome in (await session.execute(stmt)).all():
        outcomes.setdefault(profile_id, []).append(outcome)

    for profile_id, outs in outcomes.items():
        wins = sum(1 for o in outs if o == MatchOutcome.WIN)
        # `outs` is newest-first; the streak is the leading run of one outcome.
        lead = outs[0]
        run = 0
        for outcome in outs:
            if outcome != lead:
                break
            run += 1
        records[profile_id] = TournamentRecord(
            games_played=len(outs),
            wins=wins,
            losses=len(outs) - wins,
            streak=run if lead == MatchOutcome.WIN else -run,
        )
    return records


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
    tournament_records = await _tournament_record_by_profile(session, tournament, profile_ids)

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
                tournament_record=tournament_records[player.profile_id],
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


@router.get("/{tournament_slug}/teams/standings")
async def get_team_standings(
    response: Response,
    tournament: Tournament = Depends(get_tournament),
    session: AsyncSession = Depends(get_async_session),
) -> ListEnvelope[TeamStandingRow]:
    """The tournament's teams, ranked by combined current rating.

    A team's combined rating is the sum of its members' current ratings
    on the tournament's leaderboard; the average is that sum over the
    member count. Members without a rating on that leaderboard are
    omitted. Teams are optional — a tournament with none returns an empty
    list. Sorted by combined sum desc.
    """
    response.headers["Cache-Control"] = _STANDINGS_CACHE_CONTROL

    teams = (
        (await session.execute(select(Team).where(Team.tournament_id == tournament.id)))
        .scalars()
        .all()
    )

    member_stmt = (
        select(
            TeamMember.team_id,
            TeamMember.profile_id,
            Player.alias,
            Player.country,
            PlayerRating.current_rating,
            PlayerRating.updated_at,
        )
        .join(Team, Team.id == TeamMember.team_id)
        .join(Player, Player.profile_id == TeamMember.profile_id)
        .join(PlayerRating, PlayerRating.profile_id == TeamMember.profile_id)
        .where(
            Team.tournament_id == tournament.id,
            PlayerRating.leaderboard_id == tournament.leaderboard_id,
        )
    )
    member_rows = (await session.execute(member_stmt)).all()

    # Fetch live-match status for every member in one query, using the same
    # helper the per-player ``/standings`` endpoint uses. Sharing the helper
    # is the reason a member's ``in_match`` here matches their standings row
    # within the same poll cycle — both read from the same snapshot.
    live_match_ids = await _live_match_by_profile(session, [row.profile_id for row in member_rows])

    members_by_team: dict[int, list[TeamMemberRead]] = {}
    timestamps: list[datetime | None] = []
    for team_id, profile_id, alias, country, rating, updated_at in member_rows:
        members_by_team.setdefault(team_id, []).append(
            TeamMemberRead(
                profile_id=profile_id,
                alias=alias,
                country=country,
                current_rating=rating,
                in_match=profile_id in live_match_ids,
                live_match_id=live_match_ids.get(profile_id),
            )
        )
        timestamps.append(updated_at)

    items: list[TeamStandingRow] = []
    for team in teams:
        members = sorted(
            members_by_team.get(team.id, []),
            key=lambda m: m.current_rating,
            reverse=True,
        )
        total = sum(m.current_rating for m in members)
        count = len(members)
        items.append(
            TeamStandingRow(
                team_id=team.id,
                name=team.name,
                initials=team.initials,
                member_count=count,
                combined_rating_sum=total,
                combined_rating_average=(total / count) if count else 0.0,
                members=members,
            )
        )
    items.sort(key=lambda t: t.combined_rating_sum, reverse=True)

    return ListEnvelope[TeamStandingRow](
        last_polled_at=compute_last_polled_at(timestamps),
        items=items,
    )

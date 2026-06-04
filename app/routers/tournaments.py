"""Tournament endpoints: list, detail, per-tournament standings, and edits.

A tournament scopes the read surface — its roster (``TournamentPlayer``)
and its ``leaderboard_id`` select which players and ratings a standings
request sees. ``POST /`` is open to any authenticated criticalbit user
(the caller is recorded as the first owner); ``PATCH`` and
``DELETE /{slug}`` are owner-gated; every read route is public.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import Row, and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.audit import AuditAction, audit
from app.auth import get_current_user_id, require_tournament_owner
from app.cache import apply_live_cache_control
from app.database import get_async_session
from app.limiting import limiter
from app.models import (
    UNKNOWN_CIVILIZATION_ID,
    Civilization,
    HostLiveStream,
    LiveMatchPlayer,
    LiveStream,
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
from app.poller.broadcast import PLATFORM_TWITCH
from app.schemas import (
    CivStat,
    CivStats,
    ListEnvelope,
    PlayerCivStats,
    PlayerProgression,
    PlayerStandingHistory,
    RatingPoint,
    RecentMatchup,
    StandingHistoryPoint,
    StandingRow,
    StandingsHistory,
    StandingTeam,
    TeamMemberRead,
    TeamStandingHistory,
    TeamStandingHistoryPoint,
    TeamStandingRow,
    TournamentCreate,
    TournamentRead,
    TournamentRecord,
    TournamentUpdate,
    compute_last_polled_at,
)

router = APIRouter(prefix="/tournaments", tags=["tournaments"])

# Standings (per-player and per-team) update on the player-stats polling
# cadence (30s); 15s shared cache keeps worst-case viewer staleness around
# 45s. Admins reading right after a roster mutation get `private, no-store`
# instead — see app/cache.py for the full two-audience contract and #105
# for the symptom that motivated the auth-aware split.
_STANDINGS_CDN_SECONDS = 15

# Tournament config reads (the list + a single tournament's metadata).
# Static split-cache: CF holds for 15s, the browser always revalidates
# (`max-age=0, must-revalidate`). Config changes rarely (only on admin
# create/delete/edit), and an admin's read-after-write is kept fresh by
# the Cloudflare cookie-bypass Cache Rule (see infra/README.md) plus the
# browser revalidation — so unlike the polled-data endpoints, these
# don't need the auth-aware `app.cache.apply_live_cache_control` split.
# Since #103 the middleware default is `no-store`, so these must declare
# their cacheable posture explicitly to stay coalesced for viewers.
_TOURNAMENT_CONFIG_CACHE_CONTROL = "public, s-maxage=15, max-age=0, must-revalidate"

# How many recent win/loss outcomes each standings row carries. Most-
# recent-first; the consumer renders a compact form strip and can show
# fewer client-side.
_RECENT_RESULTS_LIMIT = 10

# A player counts as "in a match" while their live match sits in one of
# these states — mirrors the live-feed filter.
_LIVE_MATCH_STATES = (MatchState.STAGING, MatchState.IN_PROGRESS)


# Reserved slug that resolves to "the currently active tournament" —
# never matches a real row, so the create-route validator (see
# ``TournamentCreate.slug``) rejects this string to keep the alias
# unambiguous.
CURRENT_TOURNAMENT_ALIAS = "current"


async def get_tournament(
    tournament_slug: str,
    session: AsyncSession = Depends(get_async_session),
) -> Tournament:
    """Resolve the ``{tournament_slug}`` path parameter to a Tournament, or 404.

    The literal slug ``"current"`` is a tournament-agnostic alias: it
    resolves to the most recently started tournament (``start_date <=
    now`` ordered ``start_date`` desc, then ``created_at`` desc),
    falling back to the most recently created tournament if none have
    started yet. Used by external probes (Cloud Monitoring uptime, the
    Sentry uptime monitor) so the check survives tournament-to-
    tournament rollovers without an infra redeploy.
    """
    if tournament_slug == CURRENT_TOURNAMENT_ALIAS:
        has_started = Tournament.start_date <= datetime.now(UTC)
        stmt = (
            select(Tournament)
            .order_by(
                # Started rows first (0 = started, 1 = not started / null).
                case((has_started, 0), else_=1).asc(),
                # Among started rows, prefer the one most recently
                # started. For not-started rows this key is null so
                # ``created_at`` decides — keeps a future-scheduled
                # tournament from outranking a more-recently-created
                # one that just has no ``start_date`` set yet.
                case((has_started, Tournament.start_date), else_=None).desc().nulls_last(),
                Tournament.created_at.desc(),
            )
            .limit(1)
        )
    else:
        stmt = select(Tournament).where(Tournament.slug == tournament_slug)
    tournament = (await session.execute(stmt)).scalar_one_or_none()
    if tournament is None:
        raise HTTPException(status_code=404, detail="Tournament not found")
    return tournament


@router.get("")
async def list_tournaments(
    response: Response,
    session: AsyncSession = Depends(get_async_session),
) -> list[TournamentRead]:
    """Every tournament this deployment serves, newest first.

    Tournaments are configuration rather than polled data, so the response
    is a plain list — no ``last_polled_at`` envelope, and the static
    config split-cache rather than the auth-aware polled-data helper
    (see ``_TOURNAMENT_CONFIG_CACHE_CONTROL``).
    """
    response.headers["Cache-Control"] = _TOURNAMENT_CONFIG_CACHE_CONTROL
    stmt = select(Tournament).order_by(Tournament.created_at.desc())
    tournaments = (await session.execute(stmt)).scalars().all()
    live_hosts = await _host_stream_live_tournaments(session, [t.id for t in tournaments])
    return [_serialize_tournament(t, live_hosts) for t in tournaments]


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
        prize_pool_cents=payload.prize_pool_cents,
        host_stream_urls=payload.host_stream_urls,
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
    # A brand-new tournament can't already be in host_live_streams, so the
    # live set is empty — saves a round trip.
    return _serialize_tournament(tournament, set())


@router.get("/{tournament_slug}")
async def get_tournament_detail(
    response: Response,
    tournament: Tournament = Depends(get_tournament),
    session: AsyncSession = Depends(get_async_session),
) -> TournamentRead:
    """A single tournament's metadata."""
    response.headers["Cache-Control"] = _TOURNAMENT_CONFIG_CACHE_CONTROL
    live_hosts = await _host_stream_live_tournaments(session, [tournament.id])
    return _serialize_tournament(tournament, live_hosts)


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
    live_hosts = await _host_stream_live_tournaments(session, [tournament.id])
    return _serialize_tournament(tournament, live_hosts)


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


def _serialize_tournament(
    tournament: Tournament, live_host_tournaments: set[int]
) -> TournamentRead:
    """Build a TournamentRead, splicing in the derived ``host_stream_live``.

    The flag lives outside the ORM model — it's a per-request fold from
    the broadcast-live snapshot. Attaching it as a transient attribute
    keeps ``from_attributes=True`` on the schema working uniformly.
    """
    tournament.host_stream_live = tournament.id in live_host_tournaments
    return TournamentRead.model_validate(tournament)


async def _host_stream_live_tournaments(
    session: AsyncSession,
    tournament_ids: list[int],
) -> set[int]:
    """Return tournaments whose host channel is broadcasting now (any platform).

    Reads the ``host_live_streams`` snapshot the broadcast-live pollers
    rewrite each cycle (#149); a tournament with an entry on any platform
    is live. Backs ``host_stream_live`` on the tournament resource. Empty
    when no hosts are live, when the list is empty, or when detection
    is off for every tournament queried.
    """
    if not tournament_ids:
        return set()
    stmt = (
        select(HostLiveStream.tournament_id)
        .where(HostLiveStream.tournament_id.in_(tournament_ids))
        .distinct()
    )
    return set((await session.execute(stmt)).scalars().all())


class _StreamLive(NamedTuple):
    """A roster row's live-broadcast title + category, as of the last poll (#233)."""

    title: str | None
    category: str | None


async def _stream_live_roster_rows(
    session: AsyncSession,
    tournament_player_ids: list[int],
) -> dict[int, _StreamLive]:
    """Map each live roster row to its broadcast title + category (any platform).

    Reads the ``live_streams`` snapshot the broadcast-live pollers rewrite
    each cycle; a roster row present on any platform is live, and the row's
    presence in the returned map backs the ``stream_live`` flag on the
    standings row for both polled and placeholder rows (#147). When a row is
    live on both platforms, Twitch's metadata wins — it carries the
    ``category`` YouTube lacks (#233). Empty when detection is off.
    """
    if not tournament_player_ids:
        return {}
    stmt = select(
        LiveStream.tournament_player_id,
        LiveStream.platform,
        LiveStream.title,
        LiveStream.category,
    ).where(LiveStream.tournament_player_id.in_(tournament_player_ids))
    live: dict[int, _StreamLive] = {}
    for tp_id, platform, title, category in (await session.execute(stmt)).all():
        # First writer wins, except Twitch always overrides a prior YouTube row.
        if tp_id not in live or platform == PLATFORM_TWITCH:
            live[tp_id] = _StreamLive(title=title, category=category)
    return live


async def _tournament_record_by_profile(
    session: AsyncSession,
    tournament: Tournament,
    profile_ids: list[int],
    names: dict[int, str],
) -> dict[int, TournamentRecord]:
    """Map each profile to its stats within the tournament window.

    Pulls every completed match on the tournament's leaderboard whose
    ``started_at`` falls inside ``[start_date, grand_finals_date]`` — a
    null bound is treated as open — and folds it into one ``TournamentRecord``
    per profile: counts, streak, peak rating, latest-match timestamp, a
    capped recent-results list, and the matching capped recent-matchups list
    (#218, same rows projected with the civ matchup). Every profile gets an
    entry; those with no in-window matches get a zero record (counts/streak
    0, others null/empty).
    """
    records = {
        profile_id: TournamentRecord(
            games_played=0,
            wins=0,
            losses=0,
            streak=0,
            peak_rating=None,
            last_match_at=None,
            recent_results=[],
            recent_matchups=[],
        )
        for profile_id in profile_ids
    }
    if not profile_ids:
        return records

    # Opponent civ for the recent-matchup tooltip (#218): a correlated scalar
    # subquery, NOT a join — a join would fan the entrant's match rows out
    # one-per-opponent and corrupt the counts/streak folded from the same
    # rows. One opponent per match on a 1v1 leaderboard (this event); the
    # `order_by` keeps a team-game leaderboard's pick deterministic.
    opponent = aliased(MatchPlayer)
    opponent_civilization_id = (
        select(opponent.civilization_id)
        .where(
            opponent.match_id == MatchPlayer.match_id,
            opponent.team_id != MatchPlayer.team_id,
        )
        .order_by(opponent.profile_id)
        .limit(1)
        .scalar_subquery()
    )

    stmt = (
        select(
            MatchPlayer.profile_id,
            MatchPlayer.outcome,
            MatchPlayer.new_rating,
            MatchPlayer.civilization_id,
            Match.started_at,
            Match.completed_at,
            Match.map_name,
            opponent_civilization_id.label("opponent_civilization_id"),
        )
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

    rows_by_profile: dict[int, list[Row]] = {}
    for row in (await session.execute(stmt)).all():
        rows_by_profile.setdefault(row.profile_id, []).append(row)

    for profile_id, rows in rows_by_profile.items():
        outs = [r.outcome for r in rows]
        wins = sum(1 for o in outs if o == MatchOutcome.WIN)
        # `outs` is newest-first; the streak is the leading run of one outcome.
        lead = outs[0]
        run = 0
        for outcome in outs:
            if outcome != lead:
                break
            run += 1
        ratings = [r.new_rating for r in rows if r.new_rating is not None]
        records[profile_id] = TournamentRecord(
            games_played=len(outs),
            wins=wins,
            losses=len(outs) - wins,
            streak=run if lead == MatchOutcome.WIN else -run,
            peak_rating=max(ratings) if ratings else None,
            # `rows` is newest-first; row 0's started_at is the latest.
            last_match_at=rows[0].started_at,
            recent_results=outs[:_RECENT_RESULTS_LIMIT],
            recent_matchups=[
                RecentMatchup(
                    outcome=r.outcome,
                    civilization_id=r.civilization_id,
                    civilization_name=names.get(r.civilization_id),
                    opponent_civilization_id=r.opponent_civilization_id,
                    opponent_civilization_name=(
                        names.get(r.opponent_civilization_id)
                        if r.opponent_civilization_id is not None
                        else None
                    ),
                    map_name=r.map_name,
                    completed_at=r.completed_at,
                )
                for r in rows[:_RECENT_RESULTS_LIMIT]
            ],
        )
    return records


async def _team_by_tournament_player(
    session: AsyncSession,
    tournament_id: int,
) -> dict[int, StandingTeam]:
    """Map each teamed roster row to its team within the tournament.

    Keyed on ``tournament_player_id`` — the surrogate ``team_members``
    already keys on (#181) and every standings row carries (#184) — so an
    *unlinked* member (a roster row with no ``profile_id`` yet) surfaces
    its team like a linked one. Keying on ``profile_id``
    instead stranded such members at ``team = null`` even when rostered on
    a team (#187): the join back to ``TournamentPlayer`` and its
    ``profile_id IS NOT NULL`` filter both dropped them.

    Scoped to the tournament's teams; a roster row appears at most once,
    since a player belongs to at most one team per tournament. Rows on no
    team are absent from the map — the caller renders their standings row
    with ``team = null``.
    """
    stmt = (
        select(TeamMember.tournament_player_id, Team.id, Team.name, Team.initials)
        .select_from(TeamMember)
        .join(Team, Team.id == TeamMember.team_id)
        .where(Team.tournament_id == tournament_id)
    )
    rows = (await session.execute(stmt)).all()
    return {
        tournament_player_id: StandingTeam(team_id=team_id, name=name, initials=initials)
        for tournament_player_id, team_id, name, initials in rows
    }


async def _presentation_by_profile(
    session: AsyncSession,
    tournament_id: int,
) -> dict[int, dict]:
    """Map each roster profile to its presentation bag for the tournament.

    Reads the organizer-curated ``presentation`` on ``tournament_players``
    — an opaque dict the API doesn't interpret. Every roster row has one
    (default ``{}``); a profile absent from the map gets ``{}`` from the
    caller.
    """
    stmt = select(TournamentPlayer.profile_id, TournamentPlayer.presentation).where(
        TournamentPlayer.tournament_id == tournament_id,
    )
    rows = (await session.execute(stmt)).all()
    return dict(rows)


@router.get("/{tournament_slug}/standings")
async def get_standings(
    request: Request,
    response: Response,
    tournament: Tournament = Depends(get_tournament),
    session: AsyncSession = Depends(get_async_session),
) -> ListEnvelope[StandingRow]:
    """The tournament's roster — rated rows ranked by peak, then every other row by name.

    One query over ``tournament_players`` left-joined to ``Player`` (the
    polled identity, when one is linked) and ``PlayerRating`` (when that
    identity has a rating on the tournament's leaderboard), ordered by:

    1. rated rows by peak (``max_rating``) DESC (NULLS LAST), then
       ``current_rating`` DESC and ``name`` as tie-breaks;
    2. then every unrated row — linked or not — by ``name`` ASC.

    Position is by peak so it matches the table's ``comparePeakRank`` and
    ``/standings/history`` (#226): peak elo is carried in and only ever rises
    on a new all-time high. Both ``current_rating`` and ``max_rating`` are
    returned, so a tournament can rank on either. (#187 unified the old
    three-tier sort; #226 switched the rank key from current_rating to peak.)

    The leaderboard filter lives in the join condition, not the WHERE
    clause — putting it in WHERE would re-filter the outer-join right
    back to inner-join behaviour. The outer filter keeps a linked entry
    visible only once its ``Player`` row has been polled (no half-state
    where a newly-linked profile_id surfaces without an alias).
    """
    apply_live_cache_control(request, response, cdn_seconds=_STANDINGS_CDN_SECONDS)

    stmt = (
        select(TournamentPlayer, Player, PlayerRating)
        .outerjoin(Player, TournamentPlayer.profile_id == Player.profile_id)
        .outerjoin(
            PlayerRating,
            and_(
                PlayerRating.profile_id == Player.profile_id,
                PlayerRating.leaderboard_id == tournament.leaderboard_id,
            ),
        )
        .where(
            TournamentPlayer.tournament_id == tournament.id,
            # Linked-row visibility gate (see docstring): a linked entry
            # only surfaces once its ``Player`` row exists. Unlinked
            # rows pass through this OR via the right disjunct.
            or_(Player.profile_id.is_not(None), TournamentPlayer.profile_id.is_(None)),
        )
        .order_by(
            # Position ranks by peak (``max_rating``), matching the table's
            # comparePeakRank and ``/standings/history`` (#226) — peak elo is
            # carried in and immutable bar a new all-time high, which is what
            # the team balancing is built around. ``current_rating`` and
            # ``name`` only break ties; both ratings are returned so a
            # tournament can rank either way. (Was current_rating before #226.)
            PlayerRating.max_rating.desc().nulls_last(),
            PlayerRating.current_rating.desc().nulls_last(),
            TournamentPlayer.name.asc(),
        )
    )
    rows = (await session.execute(stmt)).all()

    profile_ids = [entry.profile_id for entry, _, _ in rows if entry.profile_id is not None]
    roster_row_ids = [entry.id for entry, _, _ in rows]
    recent_results = await _recent_results_by_profile(
        session, tournament.leaderboard_id, profile_ids
    )
    live_match_ids = await _live_match_by_profile(session, profile_ids)
    civilization_names = await _civilization_names(session)
    tournament_records = await _tournament_record_by_profile(
        session, tournament, profile_ids, civilization_names
    )
    teams_by_tournament_player = await _team_by_tournament_player(session, tournament.id)
    stream_live_rows = await _stream_live_roster_rows(session, roster_row_ids)

    items: list[StandingRow] = []
    timestamps: list[datetime | None] = []
    for entry, player, rating in rows:
        stream = stream_live_rows.get(entry.id)
        if player is not None:
            items.append(
                StandingRow(
                    tournament_player_id=entry.id,
                    profile_id=player.profile_id,
                    name=entry.name,
                    alias=player.alias,
                    country=player.country,
                    team=teams_by_tournament_player.get(entry.id),
                    presentation=entry.presentation,
                    current_rating=rating.current_rating if rating else None,
                    max_rating=rating.max_rating if rating else None,
                    wins=rating.wins if rating else 0,
                    losses=rating.losses if rating else 0,
                    streak=rating.streak if rating else 0,
                    recent_results=recent_results.get(player.profile_id, []),
                    tournament_record=tournament_records[player.profile_id],
                    rank=rating.rank if rating else None,
                    rank_total=rating.rank_total if rating else None,
                    in_match=player.profile_id in live_match_ids,
                    live_match_id=live_match_ids.get(player.profile_id),
                    stream_live=stream is not None,
                    stream_title=stream.title if stream else None,
                    stream_category=stream.category if stream else None,
                    last_match_at=rating.last_match_at if rating else None,
                    updated_at=rating.updated_at if rating else player.updated_at,
                )
            )
            timestamps.append(player.updated_at)
            timestamps.append(rating.updated_at if rating else None)
        else:
            items.append(
                StandingRow(
                    tournament_player_id=entry.id,
                    profile_id=None,
                    name=entry.name,
                    alias=entry.name,
                    country=None,
                    team=teams_by_tournament_player.get(entry.id),
                    presentation=entry.presentation,
                    current_rating=None,
                    max_rating=None,
                    wins=0,
                    losses=0,
                    streak=0,
                    recent_results=[],
                    tournament_record=TournamentRecord(
                        games_played=0,
                        wins=0,
                        losses=0,
                        streak=0,
                        peak_rating=None,
                        last_match_at=None,
                        recent_results=[],
                        recent_matchups=[],
                    ),
                    rank=None,
                    rank_total=None,
                    in_match=False,
                    live_match_id=None,
                    stream_live=stream is not None,
                    stream_title=stream.title if stream else None,
                    stream_category=stream.category if stream else None,
                    last_match_at=None,
                    updated_at=None,
                )
            )

    return ListEnvelope[StandingRow](
        last_polled_at=compute_last_polled_at(timestamps),
        items=items,
    )


async def _civilization_names(session: AsyncSession) -> dict[int, str]:
    """Map ``civilization_id -> name`` from the civilizations reference table.

    Folded onto each civ id the read endpoints return (#227), so a consumer
    doesn't maintain its own id→name list. ``.get(id)`` yields ``None`` for an
    id not in the reference (a brand-new civ before the next refresh, or the
    missing-civ sentinel) — callers surface that as a null name.
    """
    rows = await session.execute(select(Civilization.civilization_id, Civilization.name))
    return dict(rows.all())


def _sorted_civ_stats(counts: dict[int, list[int]], names: dict[int, str]) -> list[CivStat]:
    """Build a ``CivStat`` list from ``{civ_id: [picks, wins]}``.

    Ordered most-played first, then civ id for a stable tie-break — the
    shared ordering of every civ list (``/civ-stats`` and team civs, #220).
    ``names`` folds in the display name (#227); a civ id absent from it gets
    a null name.
    """
    return [
        CivStat(civilization_id=civ_id, name=names.get(civ_id), picks=picks, wins=wins)
        for civ_id, (picks, wins) in sorted(counts.items(), key=lambda kv: (-kv[1][0], kv[0]))
    ]


async def _civ_counts_by_profile(
    session: AsyncSession,
    tournament: Tournament,
    profile_ids: list[int],
) -> tuple[dict[int, dict[int, list[int]]], datetime | None]:
    """Per-profile civ pick/win counts, in-window, on the tournament's leaderboard.

    Returns ``(counts, last_completed_at)``: ``counts`` maps each profile to
    ``{civilization_id: [picks, wins]}`` over its completed in-window matches
    (picks = games on a civ, wins = the subset won); ``last_completed_at`` is
    the most recent counted match's completion time (the freshness signal).
    Shared by ``/civ-stats`` and the per-team civ aggregate (#220).
    """
    if not profile_ids:
        return {}, None

    stmt = (
        select(
            MatchPlayer.profile_id,
            MatchPlayer.civilization_id,
            func.count().label("picks"),
            func.sum(case((MatchPlayer.outcome == MatchOutcome.WIN, 1), else_=0)).label("wins"),
            func.max(Match.completed_at).label("last_completed_at"),
        )
        .join(Match, Match.match_id == MatchPlayer.match_id)
        .where(
            Match.leaderboard_id == tournament.leaderboard_id,
            MatchPlayer.profile_id.in_(profile_ids),
            MatchPlayer.outcome.is_not(None),
            # Skip only the missing-civ sentinel (-1), not civ 0 — id 0 is
            # Armenians, a real civ (see _UNKNOWN_CIVILIZATION_ID in
            # parse_recent_matches). The game still counts toward W/L; we just
            # don't attribute an unknown civ to a junk bucket.
            MatchPlayer.civilization_id != UNKNOWN_CIVILIZATION_ID,
        )
        .group_by(MatchPlayer.profile_id, MatchPlayer.civilization_id)
    )
    if tournament.start_date is not None:
        stmt = stmt.where(Match.started_at >= tournament.start_date)
    if tournament.grand_finals_date is not None:
        stmt = stmt.where(Match.started_at <= tournament.grand_finals_date)

    counts: dict[int, dict[int, list[int]]] = {}
    timestamps: list[datetime | None] = []
    for profile_id, civ_id, picks, wins, last_completed_at in (await session.execute(stmt)).all():
        counts.setdefault(profile_id, {})[civ_id] = [picks, wins]
        timestamps.append(last_completed_at)
    return counts, compute_last_polled_at(timestamps)


@router.get("/{tournament_slug}/civ-stats")
async def get_civ_stats(
    request: Request,
    response: Response,
    tournament: Tournament = Depends(get_tournament),
    session: AsyncSession = Depends(get_async_session),
) -> CivStats:
    """Civilization pick/win aggregation for the tournament's entrants.

    Counts only the tournament players' completed matches on the tournament's
    leaderboard, windowed to ``[start_date, grand_finals_date]`` (a null bound
    is open) — their ladder opponents' civ rows are excluded. ``overall``
    aggregates across all entrants; ``by_player`` breaks the same counts down
    per roster row. ``picks`` is the completed games on a civ, ``wins`` the
    subset won; civs with no entrant picks are absent.
    """
    apply_live_cache_control(request, response, cdn_seconds=_STANDINGS_CDN_SECONDS)

    # Entrants are linked roster rows (profile_id set). Unlinked rows have no
    # polled identity and thus no match data — the IN filter excludes them.
    roster = (
        await session.execute(
            select(TournamentPlayer.id, TournamentPlayer.profile_id).where(
                TournamentPlayer.tournament_id == tournament.id,
                TournamentPlayer.profile_id.is_not(None),
            )
        )
    ).all()
    tournament_player_id_by_profile = {profile_id: tp_id for tp_id, profile_id in roster}
    profile_ids = list(tournament_player_id_by_profile)
    if not profile_ids:
        return CivStats(last_polled_at=None, overall=[], by_player=[])

    counts, last_polled_at = await _civ_counts_by_profile(session, tournament, profile_ids)
    names = await _civilization_names(session)

    # Fold the per-profile counts into the cross-entrant overall sum.
    overall: dict[int, list[int]] = {}  # civ_id -> [picks, wins]
    for civs in counts.values():
        for civ_id, (picks, wins) in civs.items():
            tally = overall.setdefault(civ_id, [0, 0])
            tally[0] += picks
            tally[1] += wins

    by_player = sorted(
        (
            PlayerCivStats(
                tournament_player_id=tournament_player_id_by_profile[profile_id],
                profile_id=profile_id,
                civs=_sorted_civ_stats(civs, names),
            )
            for profile_id, civs in counts.items()
        ),
        key=lambda player: player.tournament_player_id,
    )

    return CivStats(
        last_polled_at=last_polled_at,
        overall=_sorted_civ_stats(overall, names),
        by_player=by_player,
    )


@router.get("/{tournament_slug}/progression")
async def get_progression(
    request: Request,
    response: Response,
    tournament: Tournament = Depends(get_tournament),
    session: AsyncSession = Depends(get_async_session),
) -> ListEnvelope[PlayerProgression]:
    """Per-player rating-over-time for the tournament's roster.

    One series per roster player who has completed-match history on the
    tournament's leaderboard: a list of ``(completed_at, rating)`` points
    oldest-first, where ``rating`` is the post-match value. The consumer
    plots rating against ``completed_at`` for a by-date view, or against
    point index for a by-games-played view. Players with no such history
    are omitted. Points are bounded by the tournament's date window
    (``[start_date, grand_finals_date]``; a null bound is open), mirroring
    ``tournament_record`` — so the chart reflects in-event rating movement,
    not a player's whole tracked history.
    """
    apply_live_cache_control(request, response, cdn_seconds=_STANDINGS_CDN_SECONDS)

    stmt = (
        select(
            TournamentPlayer.id,
            Player.profile_id,
            Player.alias,
            Match.completed_at,
            MatchPlayer.new_rating,
        )
        .join(MatchPlayer, MatchPlayer.profile_id == Player.profile_id)
        .join(Match, Match.match_id == MatchPlayer.match_id)
        # Join the roster row (at most one per profile per tournament) — scopes
        # to the roster AND yields its tournament_player_id, the stable series
        # key (#187), replacing the old profile_id IN-subquery.
        .join(
            TournamentPlayer,
            and_(
                TournamentPlayer.profile_id == Player.profile_id,
                TournamentPlayer.tournament_id == tournament.id,
            ),
        )
        .where(
            Match.leaderboard_id == tournament.leaderboard_id,
            Match.completed_at.is_not(None),
            MatchPlayer.new_rating.is_not(None),
        )
        # Alpha by alias for a stable legend; chronological within a player.
        .order_by(Player.alias, Player.profile_id, Match.completed_at, Match.match_id)
    )
    # Window to the tournament's date bounds, mirroring `_tournament_record_by_profile`,
    # so the chart reflects in-event rating movement rather than a player's whole tracked
    # history (which can reach back years). A null bound is treated as open.
    if tournament.start_date is not None:
        stmt = stmt.where(Match.started_at >= tournament.start_date)
    if tournament.grand_finals_date is not None:
        stmt = stmt.where(Match.started_at <= tournament.grand_finals_date)
    rows = (await session.execute(stmt)).all()

    series: dict[int, PlayerProgression] = {}
    timestamps: list[datetime | None] = []
    for tournament_player_id, profile_id, alias, completed_at, rating in rows:
        player_series = series.get(profile_id)
        if player_series is None:
            player_series = PlayerProgression(
                tournament_player_id=tournament_player_id,
                profile_id=profile_id,
                alias=alias,
                points=[],
            )
            series[profile_id] = player_series
        player_series.points.append(RatingPoint(completed_at=completed_at, rating=rating))
        timestamps.append(completed_at)

    return ListEnvelope[PlayerProgression](
        last_polled_at=compute_last_polled_at(timestamps),
        items=list(series.values()),
    )


def _to_utc(value: datetime) -> datetime:
    """Normalize a possibly-naive DB datetime to aware UTC.

    Postgres (asyncpg) returns tz-aware datetimes, but SQLite (tests) hands
    back naive ones for ``DateTime(timezone=True)`` columns. The history
    endpoint does calendar-date arithmetic on these, so they must be aware
    and on a common zone first.
    """
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


@router.get("/{tournament_slug}/standings/history")
async def get_standings_history(
    request: Request,
    response: Response,
    tournament: Tournament = Depends(get_tournament),
    session: AsyncSession = Depends(get_async_session),
) -> StandingsHistory:
    """Every roster entity's standings position over time (#219, #226).

    A bump chart. Each bucket is a snapshot "as of" its timestamp, emitted at a
    **daily anchor** (midnight UTC) and **at every position shift** (stamped at
    the match-completion time that caused it) — so quiet days still show a
    point and every reorder is captured. Every roster entity holds a
    ``position`` at every bucket, ranked the same way the live table is — by
    peak (``max_rating``) desc, then current rating, then name
    (``comparePeakRank``). Unrated members are included and rank at the tail by
    name, so the chart is complete (everyone has a line). The roster is gated
    identically to ``/standings`` (#232) — a row linked to a not-yet-polled
    ``profile_id`` is held back — so the two surfaces always agree on the
    entity set and the chart never carries a phantom the FE can't label.

    Peak elo is carried in and only rises on a new all-time high, so an
    entity's ``peak_rating`` as of a bucket is ``max(pre-event baseline,
    in-window peak-so-far)`` — flat at their lifetime peak unless they set a
    new high mid-event. The baseline is the current ``max_rating`` when it tops
    every in-window rating (the common case, exact); the in-window series comes
    from the immutable match log (same source as ``/progression``), windowed to
    the tournament dates. So the latest bucket equals the live ``/standings``
    order and past buckets stay stable. Teams (``teams[].points``) rank by
    combined peak (sum of members' as-of-bucket ``max_rating``), matching the
    Teams page.
    """
    apply_live_cache_control(request, response, cdn_seconds=_STANDINGS_CDN_SECONDS)

    # Full roster with carried-in peak/current — the metric the table ranks
    # on. LEFT JOIN PlayerRating so unrated members are included (they rank at
    # the tail by name); everyone holds a position at every bucket (#226).
    # The Player join + visibility gate mirror ``/standings`` exactly (#232):
    # a row linked to a ``profile_id`` whose ``Player`` hasn't been polled yet
    # is held back, so history charts the same entity set the table shows and
    # never emits a phantom the FE can't label. Unlinked rows
    # (``profile_id IS NULL``) pass through via the right disjunct.
    roster = (
        await session.execute(
            select(
                TournamentPlayer.id,
                TournamentPlayer.profile_id,
                TournamentPlayer.name,
                PlayerRating.max_rating,
                PlayerRating.current_rating,
            )
            .outerjoin(Player, TournamentPlayer.profile_id == Player.profile_id)
            .outerjoin(
                PlayerRating,
                and_(
                    PlayerRating.profile_id == TournamentPlayer.profile_id,
                    PlayerRating.leaderboard_id == tournament.leaderboard_id,
                ),
            )
            .where(
                TournamentPlayer.tournament_id == tournament.id,
                or_(Player.profile_id.is_not(None), TournamentPlayer.profile_id.is_(None)),
            )
        )
    ).all()
    if not roster:
        return StandingsHistory(last_polled_at=None, buckets=[], players=[], teams=[])

    name_by_tp = {r.id: r.name for r in roster}
    profile_by_tp = {r.id: r.profile_id for r in roster}
    cur_max_by_tp = {r.id: r.max_rating for r in roster}
    cur_rating_by_tp = {r.id: r.current_rating for r in roster}
    tp_by_profile = {r.profile_id: r.id for r in roster if r.profile_id is not None}

    # In-event rating series per entrant (windowed), oldest-first — the only
    # thing that moves a peak during the event (a new all-time high).
    points_by_tp: dict[int, list[tuple[datetime, int]]] = {}
    if tp_by_profile:
        pts_stmt = (
            select(MatchPlayer.profile_id, Match.completed_at, MatchPlayer.new_rating)
            .join(Match, Match.match_id == MatchPlayer.match_id)
            .where(
                Match.leaderboard_id == tournament.leaderboard_id,
                MatchPlayer.profile_id.in_(list(tp_by_profile)),
                Match.completed_at.is_not(None),
                MatchPlayer.new_rating.is_not(None),
            )
            .order_by(Match.completed_at, Match.match_id)
        )
        if tournament.start_date is not None:
            pts_stmt = pts_stmt.where(Match.started_at >= tournament.start_date)
        if tournament.grand_finals_date is not None:
            pts_stmt = pts_stmt.where(Match.started_at <= tournament.grand_finals_date)
        for profile_id, completed_at, new_rating in (await session.execute(pts_stmt)).all():
            points_by_tp.setdefault(tp_by_profile[profile_id], []).append(
                (_to_utc(completed_at), new_rating)
            )

    # Time axis: a daily anchor at each midnight in the window + a marker at
    # every match completion (a potential reorder). Each bucket is "as of" its
    # timestamp (matches with completed_at <= it); a bucket is emitted at a
    # daily anchor, whenever the order changes, or at the final time — so quiet
    # days still show a point and every shift gets one (#226).
    in_event_times = [pt[0] for pts in points_by_tp.values() for pt in pts]
    if tournament.start_date is not None:
        start_anchor = _to_utc(tournament.start_date)
    elif in_event_times:
        start_anchor = min(in_event_times)
    else:
        return StandingsHistory(last_polled_at=None, buckets=[], players=[], teams=[])
    end_day = max(in_event_times).date() if in_event_times else start_anchor.date()
    daily_anchors: set[datetime] = set()
    day = min(start_anchor.date(), end_day)
    while day <= end_day:
        daily_anchors.add(datetime(day.year, day.month, day.day, tzinfo=UTC))
        day += timedelta(days=1)
    candidate_times = sorted(daily_anchors | set(in_event_times))

    members_by_team: dict[int, list[int]] = {}
    for team_id, tp_id in (
        await session.execute(
            select(Team.id, TeamMember.tournament_player_id)
            .join(TeamMember, TeamMember.team_id == Team.id)
            .where(Team.tournament_id == tournament.id)
        )
    ).all():
        members_by_team.setdefault(team_id, []).append(tp_id)

    # Carried-in peak baseline per entrant: the current max_rating when it tops
    # every in-event rating (set pre-event → flat line), else 0 (a new high was
    # set in-event, so the in-window climb tracks it). #226.
    baseline_by_tp: dict[int, int] = {}
    for tp_id in name_by_tp:
        cur_max = cur_max_by_tp[tp_id]
        in_event_total = max((r for _, r in points_by_tp.get(tp_id, [])), default=None)
        baseline_by_tp[tp_id] = (
            cur_max
            if cur_max is not None and (in_event_total is None or cur_max > in_event_total)
            else 0
        )

    # Sweep the candidate times once, advancing each entrant's in-event points,
    # ranking the full roster (and teams) at each, and emitting on order change.
    idx_by_tp = dict.fromkeys(name_by_tp, 0)
    run_peak: dict[int, int | None] = dict.fromkeys(name_by_tp)
    run_cur: dict[int, int | None] = dict.fromkeys(name_by_tp)
    buckets: list[datetime] = []
    player_points: dict[int, list[StandingHistoryPoint]] = {tp_id: [] for tp_id in name_by_tp}
    team_points: dict[int, list[TeamStandingHistoryPoint]] = {tid: [] for tid in members_by_team}
    last_vector: tuple | None = None
    for position_index, snapshot_time in enumerate(candidate_times):
        for tp_id in name_by_tp:
            points = points_by_tp.get(tp_id, [])
            cursor = idx_by_tp[tp_id]
            while cursor < len(points) and points[cursor][0] <= snapshot_time:
                run_cur[tp_id] = points[cursor][1]
                rp = run_peak[tp_id]
                run_peak[tp_id] = run_cur[tp_id] if rp is None else max(rp, run_cur[tp_id])
                cursor += 1
            idx_by_tp[tp_id] = cursor
        # peak/current as-of this time for every entrant.
        peak_at: dict[int, int | None] = {}
        cur_at: dict[int, int | None] = {}
        for tp_id in name_by_tp:
            rp = run_peak[tp_id]
            peak_at[tp_id] = (
                max(baseline_by_tp[tp_id], rp) if rp is not None else cur_max_by_tp[tp_id]
            )
            cur_at[tp_id] = (
                run_cur[tp_id] if run_cur[tp_id] is not None else cur_rating_by_tp[tp_id]
            )
        player_order = sorted(
            name_by_tp,
            key=lambda tp_id: (
                peak_at[tp_id] is None,
                -(peak_at[tp_id] or 0),
                cur_at[tp_id] is None,
                -(cur_at[tp_id] or 0),
                name_by_tp[tp_id],
                tp_id,
            ),
        )
        player_pos = {tp_id: rank for rank, tp_id in enumerate(player_order, start=1)}
        team_combined = {
            tid: sum(peak_at[tp] or 0 for tp in members_by_team[tid] if tp in peak_at)
            for tid in members_by_team
        }
        team_order = sorted(members_by_team, key=lambda tid: (-team_combined[tid], tid))
        team_pos = {tid: rank for rank, tid in enumerate(team_order, start=1)}
        # Position vector keyed by entity (fixed order) — detects any reorder.
        vector = (
            tuple(player_pos[tp_id] for tp_id in sorted(name_by_tp)),
            tuple(team_pos[tid] for tid in sorted(members_by_team)),
        )
        is_last = position_index == len(candidate_times) - 1
        if snapshot_time in daily_anchors or vector != last_vector or is_last:
            buckets.append(snapshot_time)
            for tp_id in name_by_tp:
                player_points[tp_id].append(
                    StandingHistoryPoint(position=player_pos[tp_id], peak_rating=peak_at[tp_id])
                )
            for tid in members_by_team:
                team_points[tid].append(
                    TeamStandingHistoryPoint(
                        position=team_pos[tid], combined_peak_elo=team_combined[tid]
                    )
                )
            last_vector = vector

    players = sorted(
        (
            PlayerStandingHistory(
                tournament_player_id=tp_id,
                profile_id=profile_by_tp[tp_id],
                points=player_points[tp_id],
            )
            for tp_id in name_by_tp
        ),
        key=lambda player: player.tournament_player_id,
    )
    teams = sorted(
        (
            TeamStandingHistory(team_id=team_id, points=team_points[team_id])
            for team_id in members_by_team
        ),
        key=lambda team: team.team_id,
    )

    return StandingsHistory(
        last_polled_at=max(in_event_times) if in_event_times else None,
        buckets=buckets,
        players=players,
        teams=teams,
    )


@router.get("/{tournament_slug}/teams/standings")
async def get_team_standings(
    request: Request,
    response: Response,
    tournament: Tournament = Depends(get_tournament),
    session: AsyncSession = Depends(get_async_session),
) -> ListEnvelope[TeamStandingRow]:
    """The tournament's teams, ranked by combined peak rating.

    A team's combined rating is the sum of its members' peak (lifetime
    ``max_rating``) ratings on the tournament's leaderboard; the average
    is that sum over the count of members with a non-null peak. Every
    ``team_members`` row is returned regardless of whether the poller
    has rated the member yet — a linked-but-unrated member (no
    ``PlayerRating`` row on the leaderboard, or no ``Player`` row at all
    if the poller hasn't picked them up) is listed under ``members``
    with null rating fields and excluded from the aggregate (and the
    average's denominator). Teams are optional — a tournament with none
    returns an empty list. Sorted by combined sum desc.

    Each row also carries the team's combined in-window win/loss (sum of the
    members' ``tournament_record`` W/L, plus a server-computed ``win_pct``)
    and a per-team civ pick/win aggregate, with the same per-member figures on
    each ``TeamMemberRead`` (#220).
    """
    apply_live_cache_control(request, response, cdn_seconds=_STANDINGS_CDN_SECONDS)

    teams = (
        (await session.execute(select(Team).where(Team.tournament_id == tournament.id)))
        .scalars()
        .all()
    )

    # Join through ``TournamentPlayer`` so placeholder roster rows
    # (which have no ``profile_id``) surface alongside polled identities,
    # then LEFT JOIN ``Player`` + ``PlayerRating`` so members the poller
    # hasn't rated yet are listed with null rating fields. Leaderboard
    # filter sits in the JOIN ON condition, not WHERE — moving it to
    # WHERE would re-filter the outer join back to inner-join behaviour
    # and re-introduce the #166 bug.
    member_stmt = (
        select(
            TeamMember.team_id,
            TournamentPlayer.id.label("tournament_player_id"),
            TournamentPlayer.profile_id,
            TournamentPlayer.name,
            Player.alias,
            Player.country,
            PlayerRating.current_rating,
            PlayerRating.max_rating,
            PlayerRating.updated_at,
            TeamMember.is_captain,
        )
        .join(Team, Team.id == TeamMember.team_id)
        .join(TournamentPlayer, TournamentPlayer.id == TeamMember.tournament_player_id)
        .outerjoin(Player, Player.profile_id == TournamentPlayer.profile_id)
        .outerjoin(
            PlayerRating,
            and_(
                PlayerRating.profile_id == TournamentPlayer.profile_id,
                PlayerRating.leaderboard_id == tournament.leaderboard_id,
            ),
        )
        .where(Team.tournament_id == tournament.id)
    )
    member_rows = (await session.execute(member_stmt)).all()

    # Fetch live-match status for every polled member in one query,
    # using the same helper the per-player ``/standings`` endpoint
    # uses. Sharing the helper is why a member's ``in_match`` here
    # matches their standings row within the same poll cycle — both
    # read from the same snapshot. Unlinked rows (no profile_id) can't
    # be in a live match yet.
    member_profile_ids = [row.profile_id for row in member_rows if row.profile_id is not None]
    live_match_ids = await _live_match_by_profile(session, member_profile_ids)
    # In-window win/loss per member — reuse the per-player record helper so a
    # team's W/L is exactly the sum of its members' ``tournament_record`` W/L —
    # plus per-member civ counts for the team civ aggregate (#220).
    civilization_names = await _civilization_names(session)
    records = await _tournament_record_by_profile(
        session, tournament, member_profile_ids, civilization_names
    )
    civ_counts, _ = await _civ_counts_by_profile(session, tournament, member_profile_ids)

    members_by_team: dict[int, list[TeamMemberRead]] = {}
    timestamps: list[datetime | None] = []
    for (
        team_id,
        tournament_player_id,
        profile_id,
        roster_name,
        alias,
        country,
        current_rating,
        max_rating,
        updated_at,
        is_captain,
    ) in member_rows:
        members_by_team.setdefault(team_id, []).append(
            TeamMemberRead(
                tournament_player_id=tournament_player_id,
                profile_id=profile_id,
                # Polled identities prefer ``Player.alias``; unlinked rows
                # fall back to the roster row's organizer-set ``name``.
                alias=alias if alias is not None else roster_name,
                country=country,
                current_rating=current_rating,
                max_rating=max_rating,
                in_match=profile_id is not None and profile_id in live_match_ids,
                live_match_id=live_match_ids.get(profile_id) if profile_id else None,
                is_captain=is_captain,
                # In-window W/L from the member's record; 0 for an unlinked row.
                wins=records[profile_id].wins if profile_id is not None else 0,
                losses=records[profile_id].losses if profile_id is not None else 0,
            )
        )
        timestamps.append(updated_at)

    items: list[TeamStandingRow] = []
    for team in teams:
        # Sort members by peak desc with nulls last — matches the metric
        # the headline figures and team ranking are computed on.
        members = sorted(
            members_by_team.get(team.id, []),
            key=lambda m: (m.max_rating is None, -(m.max_rating or 0)),
        )
        peaks = [m.max_rating for m in members if m.max_rating is not None]
        total = sum(peaks)
        # Per-team civ aggregate: merge the members' civ counts (#220).
        team_civ: dict[int, list[int]] = {}  # civ_id -> [picks, wins]
        for member in members:
            if member.profile_id is None:
                continue
            for civ_id, (picks, wins) in civ_counts.get(member.profile_id, {}).items():
                tally = team_civ.setdefault(civ_id, [0, 0])
                tally[0] += picks
                tally[1] += wins
        items.append(
            TeamStandingRow(
                team_id=team.id,
                name=team.name,
                initials=team.initials,
                member_count=len(members),
                combined_rating_sum=total,
                combined_rating_average=(total / len(peaks)) if peaks else 0.0,
                # Combined in-window W/L = sum of the members' records.
                combined_wins=sum(m.wins for m in members),
                combined_losses=sum(m.losses for m in members),
                civs=_sorted_civ_stats(team_civ, civilization_names),
                members=members,
            )
        )
    items.sort(key=lambda t: t.combined_rating_sum, reverse=True)

    return ListEnvelope[TeamStandingRow](
        last_polled_at=compute_last_polled_at(timestamps),
        items=items,
    )

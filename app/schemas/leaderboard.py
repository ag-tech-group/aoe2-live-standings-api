"""Leaderboard metadata + standings response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, computed_field

from app.models.match import MatchOutcome


class LeaderboardRead(BaseModel):
    """Leaderboard metadata, sourced from the ``leaderboards`` table.

    The polling worker upserts rows on startup from upstream
    ``getAvailableLeaderboards``. The minimal shape here — id, name,
    ranked flag — is enough for the consumer to render a leaderboard
    picker; richer metadata (matchtype mappings, etc.) lives on the DB
    row but is deliberately not exposed on the API.
    """

    model_config = ConfigDict(from_attributes=True)

    leaderboard_id: int
    name: str
    is_ranked: bool


class CivilizationRead(BaseModel):
    """Civilization metadata, sourced from the ``civilizations`` table.

    The polling worker upserts rows from the ``races`` array of upstream
    ``getAvailableLeaderboards``. ``civilization_id`` matches what the
    civ-stats and recent-matchup endpoints return, so a consumer maps id →
    ``name`` from this authoritative reference (#227).
    """

    model_config = ConfigDict(from_attributes=True)

    civilization_id: int
    name: str


class RecentMatchup(BaseModel):
    """One recent in-window game with its civ matchup, for a standings tooltip.

    The expand half of the recent-results enrichment (#218): the same
    outcome carried in ``TournamentRecord.recent_results`` plus the
    entrant's civ and — on a 1v1 leaderboard — the opposing player's civ,
    so the consumer can render a "<your civ> vs <their civ>" tooltip on
    each recent-result icon. The consumer maps civ ids to names/emblems.
    """

    outcome: MatchOutcome
    # The entrant's civilization in this game.
    civilization_id: int
    # The entrant's civ display name (#227), folded from the civilizations
    # reference; null when the id isn't in the reference yet (a brand-new civ
    # before the next refresh, or the missing-civ sentinel).
    civilization_name: str | None
    # The opposing player's civilization on a 1v1 leaderboard. Null when no
    # single opponent resolves — the leaderboard isn't 1v1, or the match
    # record carries no opposing-team row.
    opponent_civilization_id: int | None
    # The opponent's civ display name; null when ``opponent_civilization_id``
    # is null or its id isn't in the reference yet.
    opponent_civilization_name: str | None
    map_name: str
    # When the match finished; null only if it settled without a completion
    # time (not expected for a counted, outcome-bearing game).
    completed_at: datetime | None


class TournamentRecord(BaseModel):
    """A player's stats within a tournament's date window.

    Counts only completed matches on the tournament's leaderboard between
    its ``start_date`` and ``grand_finals_date`` (a null bound is treated as open).
    Distinct from the lifetime-ladder ``wins`` / ``losses`` / ``streak`` /
    ``max_rating`` / ``last_match_at`` / ``recent_results`` on ``StandingRow``;
    every field here is in-window only.
    """

    games_played: int
    wins: int
    losses: int
    # Positive = current win streak, negative = loss streak, 0 = no games.
    streak: int
    # Highest post-match rating (``MatchPlayer.new_rating``) the player
    # reached on completed in-window matches. Null when no in-window match
    # carried a non-null rating — either zero in-window games, or all
    # in-window games were unranked.
    peak_rating: int | None
    # Latest in-window ``Match.started_at`` for any of the player's
    # completed matches. Backs the "Active 1h / Idle 3d" badge. Null when
    # the player has no completed in-window matches.
    last_match_at: datetime | None
    # Win/loss outcomes of the player's most-recent completed in-window
    # matches, newest-first, capped server-side. Empty when no in-window
    # games. The tournament-scoped sibling of ``StandingRow.recent_results``.
    recent_results: list[MatchOutcome]
    # Expand half of the recent-results enrichment (#218): the same recent
    # in-window games, enriched with the civ matchup for a per-icon tooltip.
    # Newest-first, same cap as ``recent_results``. ``recent_results`` stays
    # through the FE transition and is removed in the contract phase once the
    # FE reads matchups instead.
    recent_matchups: list[RecentMatchup]

    @computed_field
    @property
    def win_pct(self) -> float | None:
        """Win percentage (0–100, 1 dp) over in-window games; null when none."""
        if self.games_played == 0:
            return None
        return round(self.wins / self.games_played * 100, 1)


class StandingTeam(BaseModel):
    """The team a standings row's player belongs to, if any.

    A compact reference — id + display strings, no aggregates — folded
    onto each ``StandingRow`` so the standings table can show a player's
    team where it would otherwise show their global ladder rank. A player
    belongs to at most one team per tournament; an un-teamed player's row
    carries ``team = null``.
    """

    team_id: int
    name: str
    initials: str


class StandingRow(BaseModel):
    """One row in a tournament's standings list.

    A denormalized read model: a left join of ``Player`` and ``PlayerRating``
    plus folded-in derived fields, so a consumer renders a full standings
    table from one response with no per-player fan-out. ``recent_results``
    is completed-match form; ``tournament_record`` is the player's record
    within the tournament's date window; ``in_match`` / ``live_match_id``
    are current live-match status. Sorted by ``current_rating`` desc
    (NULLS LAST), then every unrated row — linked or not — by display
    ``name`` (#187 unified the old three-tier sort that special-cased an
    unlinked tail).

    An unlinked row (no ``profile_id`` yet — a streamer whose account
    hasn't minted) carries null ``profile_id``, its ``name`` as the display
    label (``alias`` falls back to it), its ``presentation`` bag (so
    flag/streamUrls work identically), and null/zero for every polled
    field. ``updated_at`` is null too — no polled refresh signal applies.
    """

    model_config = ConfigDict(from_attributes=True)

    # The roster-row surrogate id (``tournament_players.id``) — the stable
    # management key (#167), present on every row including unlinked ones, so
    # the FE can drive team assignment (``POST /teams/{id}/members``) straight
    # off the standings without a separate lookup. Stable across an
    # unlinked row's linking to a polled identity.
    tournament_player_id: int
    # The optional enrichment link to a polled identity; null on an unlinked
    # row (#187). Address rows by tournament_player_id, not this.
    profile_id: int | None
    # The display label for this tournament — always present. ``alias`` is
    # the current polled ladder alias (enrichment) and may differ.
    name: str
    alias: str
    country: str | None
    # The player's team in this tournament, or null when they're on no
    # team. The standings table renders this in place of the global ladder
    # rank; mirrors the id + display strings on ``TeamStandingRow``.
    team: StandingTeam | None
    # Opaque per-player presentation bag (stream links, bio, etc.) set on
    # the roster and rendered by the consumer; empty object when unset. The
    # API stores it but never interprets it.
    presentation: dict
    # Null when the roster member has no rating row on this tournament's
    # leaderboard yet (e.g. a brand-new account that hasn't played its
    # first ranked match), and on unlinked rows. The other lifetime-
    # ladder fields below are 0 in those cases, and ``recent_results`` /
    # ``tournament_record`` are empty/zero.
    current_rating: int | None
    max_rating: int | None
    wins: int
    losses: int
    streak: int
    # Win/loss outcomes of the player's most recent completed matches on
    # this leaderboard, most-recent-first, capped server-side. Empty when
    # the player has no completed matches on this leaderboard yet.
    recent_results: list[MatchOutcome]
    # The player's record within this tournament's date window. The
    # sibling wins/losses/streak above are lifetime-ladder figures.
    tournament_record: TournamentRecord
    rank: int | None
    rank_total: int | None
    # True while the player is in a live (staging / in-progress) match, as
    # of the last live poll (~15s cadence). `live_match_id` is that match's
    # id when `in_match` is true, else null — for linking through to it.
    in_match: bool
    live_match_id: int | None
    # True when the player's stream (Twitch, or YouTube as a fallback) is
    # broadcasting right now, as of the last broadcast-live poll. Distinct
    # from `in_match`: a player can be streaming without being in a tracked
    # match, or vice-versa. False when detection is off or the channel is
    # offline/unknown.
    stream_live: bool
    last_match_at: datetime | None
    # Null on unlinked rows (no polled refresh applies); the row's
    # ``last_polled_at`` envelope simply doesn't consider these.
    updated_at: datetime | None

    # Derived from the lifetime-ladder wins/losses above (not the
    # tournament_record). Computed server-side so every consumer agrees on
    # the figure; the frontend still decides precision/formatting.
    @computed_field
    @property
    def games(self) -> int:
        return self.wins + self.losses

    @computed_field
    @property
    def win_pct(self) -> float | None:
        """Win percentage (0–100, 1 dp), or null when the player has no games."""
        total = self.wins + self.losses
        if total == 0:
            return None
        return round(self.wins / total * 100, 1)


class RatingPoint(BaseModel):
    """One rating observation: the player's post-match rating and when that match finished."""

    completed_at: datetime
    rating: int


class PlayerProgression(BaseModel):
    """A single player's rating-over-time series for a tournament.

    ``points`` are completed-match rating observations on the tournament's
    leaderboard, oldest-first — the consumer plots ``rating`` against
    ``completed_at`` for a by-date view, or against point index for a
    by-games-played view. A player with no completed-match history on the
    leaderboard is omitted from the series list entirely.

    ``tournament_player_id`` is the stable per-series key (#187) — a series
    only exists for a player with rated matches, so the row is always
    linked and ``profile_id`` is non-null too, but the consumer keys its
    chart on ``tournament_player_id`` for consistency with the rest of the
    read surface.
    """

    tournament_player_id: int
    profile_id: int
    alias: str
    points: list[RatingPoint]


class CivStat(BaseModel):
    """Pick/win counts for one civilization."""

    civilization_id: int
    # The civ's display name (#227), folded from the civilizations reference;
    # null when the id isn't in the reference yet (a brand-new civ before the
    # next refresh, or the missing-civ sentinel).
    name: str | None
    # Completed games an entrant played this civ (in-window, on the
    # tournament's leaderboard). Ladder opponents' rows are excluded.
    picks: int
    # The subset of ``picks`` the entrant won.
    wins: int


class PlayerCivStats(BaseModel):
    """One entrant's per-civ pick/win breakdown.

    ``tournament_player_id`` is the stable roster key (#187); ``profile_id``
    is its linked polled identity — always set, since an entry only appears
    here for a rostered player with counted matches, which requires a link.
    ``civs`` is ordered by picks desc, then civ id.
    """

    tournament_player_id: int
    profile_id: int
    civs: list[CivStat]


class CivStats(BaseModel):
    """Civilization pick/win aggregation for a tournament's entrants.

    ``overall`` sums each civ's picks/wins across all entrants; ``by_player``
    breaks the same counts down per roster row. Counts cover only the
    tournament players' completed matches on the tournament's leaderboard,
    windowed to ``[start_date, grand_finals_date]`` (a null bound is open) —
    their ladder opponents' rows are excluded. Civs with no entrant picks
    are absent from both lists. ``overall`` is ordered by picks desc then
    civ id; ``by_player`` by ``tournament_player_id``.
    """

    last_polled_at: datetime | None
    overall: list[CivStat]
    by_player: list[PlayerCivStats]


class StandingHistoryPoint(BaseModel):
    """An entity's standings position + peak rating at one time bucket."""

    # 1-based position by peak (comparePeakRank: peak desc, then current
    # rating desc, then display name), over the whole roster — everyone has a
    # position at every bucket (#226).
    position: int
    # The entity's all-time peak (``max_rating``) as of this bucket —
    # max(pre-event baseline, in-window peak-so-far). Equals the live
    # ``max_rating`` at the latest bucket; stable for past buckets. Null for an
    # unrated entity (no rating on this leaderboard) — it still holds a
    # position, at the name-sorted tail.
    peak_rating: int | None


class PlayerStandingHistory(BaseModel):
    """One entrant's position-over-time series, aligned to the shared buckets.

    ``points[i]`` is the entrant's standing at ``buckets[i]`` (see
    ``StandingsHistory``). Every entrant holds a position at every bucket
    (#226) — there are no null points; an unrated entrant simply sits at the
    name-sorted tail with a null ``peak_rating``.
    """

    tournament_player_id: int
    # Null for an unlinked roster row (no polled identity yet); the row still
    # holds a position, by name at the tail.
    profile_id: int | None
    points: list[StandingHistoryPoint]


class TeamStandingHistoryPoint(BaseModel):
    """A team's position + combined peak elo at one time bucket."""

    # 1-based position by combined peak elo (desc), over all teams.
    position: int
    # Sum of the members' all-time peak (``max_rating``) as of this bucket
    # (#226), matching the Teams page; an unrated member contributes 0.
    combined_peak_elo: int


class TeamStandingHistory(BaseModel):
    """One team's combined-peak-elo-over-time series, aligned to the buckets.

    ``points[i]`` is the team's standing at ``buckets[i]``. Every team holds a
    position at every bucket (#226) — no null points.
    """

    team_id: int
    points: list[TeamStandingHistoryPoint]


class StandingsHistory(BaseModel):
    """Tournament standings position over daily buckets (#219, #226).

    ``buckets`` is the shared daily time axis (midnight-UTC date labels).
    ``players`` and ``teams`` carry per-entity series aligned index-for-index
    to ``buckets`` — a bump chart of ``position`` over time. Every roster
    entity (rated or not) holds a position at every bucket, ranked the way the
    live table is: by all-time peak (``max_rating``) **as of each bucket**,
    then current rating, then name (``comparePeakRank``). So the latest bucket
    equals the live ``/standings`` order, and past buckets stay stable
    (peak-so-far over already-completed matches + a fixed baseline).
    """

    last_polled_at: datetime | None
    buckets: list[datetime]
    players: list[PlayerStandingHistory]
    teams: list[TeamStandingHistory]

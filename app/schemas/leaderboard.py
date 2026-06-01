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
    are current live-match status. Sorted by ``current_rating`` desc, with
    unrated roster members (no rating row on the tournament's leaderboard
    — typically brand-new accounts) next by ``profile_id``, and announced
    placeholder roster slots last by ``alias`` (their display name).

    Placeholder rows surface announced-but-unjoined entrants — streamers
    whose ``profile_id`` hasn't minted yet. ``profile_id`` is null on
    these rows (no detail page to link to), ``alias`` carries their
    display name, ``presentation`` carries their bag (so flag/streamUrls
    work identically), and every other field is null/zero. ``updated_at``
    is null too — no polled refresh signal applies.
    """

    model_config = ConfigDict(from_attributes=True)

    # Null on placeholder rows (announced-but-unjoined streamers without
    # a ``profile_id`` yet — see class docstring). Non-null on every
    # rated and unrated roster row.
    profile_id: int | None
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
    # first ranked match), and on placeholder rows. The other lifetime-
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
    # Null on placeholder rows (no polled refresh applies); the row's
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
    """

    profile_id: int
    alias: str
    points: list[RatingPoint]

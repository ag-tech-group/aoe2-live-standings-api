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
    """A player's win/loss record within a tournament's date window.

    Counts only completed matches on the tournament's leaderboard between
    its ``start_date`` and ``grand_finals_date`` (a null bound is treated as open).
    Distinct from the lifetime-ladder ``wins`` / ``losses`` / ``streak``
    on ``StandingRow``.
    """

    games_played: int
    wins: int
    losses: int
    # Positive = current win streak, negative = loss streak, 0 = no games.
    streak: int


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

    A denormalized read model: a join of ``Player`` and ``PlayerRating``
    plus folded-in derived fields, so a consumer renders a full standings
    table from one response with no per-player fan-out. ``recent_results``
    is completed-match form; ``tournament_record`` is the player's record
    within the tournament's date window; ``in_match`` / ``live_match_id``
    are current live-match status. Sorted by ``current_rating`` desc.
    """

    model_config = ConfigDict(from_attributes=True)

    profile_id: int
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
    current_rating: int
    max_rating: int
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
    last_match_at: datetime | None
    updated_at: datetime

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

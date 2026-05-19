"""Leaderboard metadata + standings response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class LeaderboardRead(BaseModel):
    """Leaderboard metadata, sourced from upstream ``getAvailableLeaderboards``.

    Until the polling worker fills the in-memory cache (see
    ``app.leaderboards_cache``), the ``/v1/leaderboards`` endpoint returns
    an empty list. The minimal shape here — id, name, ranked flag — is
    enough for the consumer to render a leaderboard picker; richer
    metadata (matchtype mappings, etc.) gets added as needed.
    """

    leaderboard_id: int
    name: str
    is_ranked: bool


class StandingRow(BaseModel):
    """One row in the standings list for a given leaderboard.

    Denormalized join of ``Player`` and ``PlayerRating`` so consumers get
    everything they need to render a standings table in one row, without
    an extra ``ratings[]`` indirection. Sorted by ``current_rating`` desc
    on the endpoint.
    """

    model_config = ConfigDict(from_attributes=True)

    profile_id: int
    alias: str
    country: str | None
    current_rating: int
    max_rating: int
    wins: int
    losses: int
    streak: int
    rank: int | None
    rank_total: int | None
    last_match_at: datetime | None
    updated_at: datetime

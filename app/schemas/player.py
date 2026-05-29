"""Player + PlayerRating response schemas."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.match import MatchRead


class PlayerRatingRead(BaseModel):
    """One PlayerRating row, embedded inside PlayerRead."""

    model_config = ConfigDict(from_attributes=True)

    leaderboard_id: int
    current_rating: int
    max_rating: int
    wins: int
    losses: int
    streak: int
    drops: int
    rank: int | None
    rank_total: int | None
    region_rank: int | None
    region_rank_total: int | None
    last_match_at: datetime | None
    updated_at: datetime


class PlayerRead(BaseModel):
    """Tracked player plus their ratings on every leaderboard we've seen them on."""

    model_config = ConfigDict(from_attributes=True)

    profile_id: int
    alias: str
    country: str | None
    steam_id: str | None
    level: int
    xp: int
    region_id: int
    clan_name: str | None
    updated_at: datetime
    # Per-tournament presentation bag, folded in by the tournament-scoped
    # roster endpoints (it lives on `tournament_players`, not `Player`).
    # Empty object when unset.
    presentation: dict = Field(default_factory=dict)
    ratings: list[PlayerRatingRead]


class PlayerDetail(PlayerRead):
    """Single-player response (``GET /v1/players/{profile_id}``).

    Extends ``PlayerRead`` with ``last_polled_at`` and the player's recent
    matches. ``recent_matches`` is set by the router (not via SQLAlchemy
    relationship) because matches are joined through ``MatchPlayer.profile_id``,
    which intentionally has no foreign key back to ``Player`` (opponents
    don't need to be tracked).
    """

    last_polled_at: datetime | None = None
    recent_matches: list[MatchRead] = []


class RosterPlayerCreate(BaseModel):
    """Request body for adding a profile to a tournament's roster."""

    profile_id: int = Field(gt=0)


_MAX_PRESENTATION_BYTES = 8192


class RosterPlayerUpdate(BaseModel):
    """Owner edit for a roster entry's presentation data.

    ``presentation`` is an opaque per-player bag the consumer renders —
    stream links, bio text, whatever the frontend defines. The API stores
    it verbatim and never interprets its keys. The body must include it (an
    empty object clears the bag); the whole object is replaced, so callers
    read-modify-write.
    """

    presentation: dict[str, Any]

    @field_validator("presentation")
    @classmethod
    def _within_size_limit(cls, value: dict[str, Any]) -> dict[str, Any]:
        # Bound the serialized size so one roster row can't bloat every
        # standings response. The API doesn't otherwise inspect the bag.
        if len(json.dumps(value).encode()) > _MAX_PRESENTATION_BYTES:
            raise ValueError(f"presentation must serialize to <= {_MAX_PRESENTATION_BYTES} bytes")
        return value

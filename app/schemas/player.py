"""Player + PlayerRating response schemas."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

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
    # The player's stream link for THIS tournament, folded in by the
    # tournament-scoped roster endpoints (the field lives on
    # `tournament_players`, not the `Player` model). Null when unset.
    stream_url: str | None = None
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


class RosterPlayerUpdate(BaseModel):
    """Owner edit for a roster entry's curated fields.

    Currently just the player's official stream link, shown in the
    standings "Watch Live" column. ``stream_url`` is required in the body
    but nullable: pass an ``http(s)`` URL to set it, or ``null`` to clear it.
    """

    stream_url: str | None = Field(max_length=2048)

    @field_validator("stream_url")
    @classmethod
    def _validate_stream_url(cls, value: str | None) -> str | None:
        # A null clears the link; any non-null value must be an absolute
        # http(s) URL. urlparse also rejects scheme-only strings like
        # `javascript:...`, since those have no netloc.
        if value is None:
            return None
        candidate = value.strip()
        parsed = urlparse(candidate)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("stream_url must be an absolute http(s) URL")
        return candidate

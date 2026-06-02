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
    """A roster entry — one tournament player, polled enrichment optional.

    ``name`` is the display label, always present. ``profile_id`` and the
    polled fields (``alias``, ``country``, ``steam_id``, ``level``, ``xp``,
    ``region_id``, ``clan_name``, ``updated_at``, ``ratings``) are populated
    from the linked ``Player`` when the entry carries a ``profile_id``; for
    an unlinked entry they're null/empty and ``alias`` falls back to
    ``name``. Display resolves to ``presentation.displayName ?? name``;
    ``alias`` is the current polled ladder alias (enrichment), which may
    differ from the tournament ``name`` (#187).
    """

    model_config = ConfigDict(from_attributes=True)

    # The roster-row surrogate id (``tournament_players.id``) — the identity
    # everything addresses (#167), and the key the team-management endpoints
    # take. Always present.
    tournament_player_id: int
    # The optional enrichment link to a polled identity; null on an unlinked
    # entry (#187). Not an identity — address rows by tournament_player_id.
    profile_id: int | None
    # The display label for this tournament — always present. Distinct from
    # ``alias`` (the current polled ladder alias, enrichment) which may
    # differ, and is the row's ``name`` falling back to nothing else.
    name: str
    alias: str
    country: str | None
    steam_id: str | None
    level: int | None
    xp: int | None
    region_id: int | None
    clan_name: str | None
    updated_at: datetime | None
    # Per-tournament presentation bag, folded in by the tournament-scoped
    # roster endpoints (it lives on `tournament_players`, not `Player`).
    # Empty object when unset.
    presentation: dict = Field(default_factory=dict)
    ratings: list[PlayerRatingRead] = Field(default_factory=list)


class PlayerDetail(PlayerRead):
    """Single-player response (``GET /v1/.../players/{tournament_player_id}``).

    Extends ``PlayerRead`` with ``last_polled_at`` and recent matches. The
    detail endpoint addresses the roster row by its surrogate
    ``tournament_player_id`` (#187), so an unlinked entry is addressable
    too — it just carries empty polled enrichment (no ``profile_id``, empty
    ``ratings`` / ``recent_matches``, null ``last_polled_at``).
    """

    last_polled_at: datetime | None = None
    recent_matches: list[MatchRead] = []


class RosterPlayerCreate(BaseModel):
    """Request body for adding a roster entry (#187 unified shape).

    ``name`` is the required display label. ``profile_id`` optionally links
    the entry to a polled identity (ratings/country/matches/live); omit it
    for an entry whose account hasn't minted yet — it stays first-class and
    can be linked later via PATCH. ``presentation`` is optional and can be
    set later via PATCH.
    """

    name: str = Field(min_length=1, max_length=64)
    profile_id: int | None = Field(default=None, gt=0)
    presentation: dict[str, Any] = Field(default_factory=dict)

    @field_validator("presentation")
    @classmethod
    def _presentation_within_size_limit(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value).encode()) > _MAX_PRESENTATION_BYTES:
            raise ValueError(f"presentation must serialize to <= {_MAX_PRESENTATION_BYTES} bytes")
        return value


_MAX_PRESENTATION_BYTES = 8192


class RosterPlayerUpdate(BaseModel):
    """Owner edit for a roster entry — presentation, and optionally link.

    ``presentation`` is an opaque per-player bag the consumer renders —
    stream links, bio text, whatever the frontend defines. The API stores
    it verbatim and never interprets its keys. The whole bag is replaced
    on PATCH, so callers read-modify-write.

    ``profile_id`` is optional: setting it **links** an unlinked entry to a
    polled identity (additive — the row's ``name`` is kept, ``profile_id``
    is set, and ``presentation`` carries through unless the body also
    supplies a new bag). An entry that's already linked can't change its
    ``profile_id`` (422 — the link is immutable once set); 409 if the
    target ``profile_id`` is already on the roster.
    """

    presentation: dict[str, Any] | None = None
    profile_id: int | None = Field(default=None, gt=0)

    @field_validator("presentation")
    @classmethod
    def _within_size_limit(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and len(json.dumps(value).encode()) > _MAX_PRESENTATION_BYTES:
            raise ValueError(f"presentation must serialize to <= {_MAX_PRESENTATION_BYTES} bytes")
        return value

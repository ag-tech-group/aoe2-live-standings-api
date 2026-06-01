"""Player + PlayerRating response schemas."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    """A roster entry — either a polled identity or an announced placeholder.

    ``profile_id`` / ``alias`` / polled fields (``country``, ``steam_id``,
    ``level``, ``xp``, ``region_id``, ``clan_name``, ``updated_at``,
    ``ratings``) are populated from a ``Player`` row when the entry has a
    polled identity. For an announced placeholder (no ``profile_id`` yet),
    ``alias`` is the host-given display name and the polled fields are
    null/empty — there's nothing to poll until the player's
    ``profile_id`` mints.
    """

    model_config = ConfigDict(from_attributes=True)

    # The roster-row surrogate id (``tournament_players.id``) — stable across a
    # placeholder's promotion to a polled identity, and the key the team-
    # management endpoints take (#167). Always present, including placeholders.
    tournament_player_id: int
    profile_id: int | None
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
    """Single-player response (``GET /v1/players/{profile_id}``).

    Extends ``PlayerRead`` with ``last_polled_at`` and the player's recent
    matches. Always represents a polled identity — the detail endpoint
    only accepts a ``profile_id``, so placeholder rows aren't addressable
    here (they have no detail to show).
    """

    last_polled_at: datetime | None = None
    recent_matches: list[MatchRead] = []


class RosterPlayerCreate(BaseModel):
    """Request body for adding a roster entry — polled identity or placeholder.

    Pass ``profile_id`` for a polled identity, or ``name`` for an
    announced placeholder. Exactly one of the two — sending both or
    neither is a 422. ``presentation`` is optional in both cases and can
    be set later via PATCH.

    ``name`` is rejected if it parses as an integer so the API can
    polymorphically dispatch URL lookups (``/players/{12345}`` →
    profile_id, ``/players/{iyouxin}`` → name) without ambiguity.
    """

    profile_id: int | None = Field(default=None, gt=0)
    name: str | None = Field(default=None, min_length=1, max_length=64)
    presentation: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name_not_numeric(cls, value: str | None) -> str | None:
        # Polymorphic URL routing (numeric → profile_id, non-numeric →
        # name) needs this disambiguation. A name like "12345" would
        # alias `/players/12345` between the two cases.
        if value is not None and value.isdigit():
            raise ValueError("name must not be an integer")
        return value

    @field_validator("presentation")
    @classmethod
    def _presentation_within_size_limit(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value).encode()) > _MAX_PRESENTATION_BYTES:
            raise ValueError(f"presentation must serialize to <= {_MAX_PRESENTATION_BYTES} bytes")
        return value

    @model_validator(mode="after")
    def _exactly_one_identity(self) -> RosterPlayerCreate:
        if (self.profile_id is None) == (self.name is None):
            raise ValueError("exactly one of profile_id or name must be set")
        return self


_MAX_PRESENTATION_BYTES = 8192


class RosterPlayerUpdate(BaseModel):
    """Owner edit for a roster entry — presentation, and optionally promote.

    ``presentation`` is an opaque per-player bag the consumer renders —
    stream links, bio text, whatever the frontend defines. The API stores
    it verbatim and never interprets its keys. The whole bag is replaced
    on PATCH, so callers read-modify-write.

    ``profile_id`` is optional and only meaningful when PATCHing a
    placeholder row: setting it **promotes** the placeholder to a polled
    identity in the same transaction (the placeholder's ``name`` is
    cleared, ``profile_id`` is set, and the row's ``presentation``
    carries through unchanged unless the body also supplies a new bag).
    A real-player row can't change its ``profile_id`` — that's the
    routing key and identity. 409 if the target ``profile_id`` is
    already on the roster.
    """

    presentation: dict[str, Any] | None = None
    profile_id: int | None = Field(default=None, gt=0)

    @field_validator("presentation")
    @classmethod
    def _within_size_limit(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and len(json.dumps(value).encode()) > _MAX_PRESENTATION_BYTES:
            raise ValueError(f"presentation must serialize to <= {_MAX_PRESENTATION_BYTES} bytes")
        return value

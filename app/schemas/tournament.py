"""Tournament request/response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TournamentRead(BaseModel):
    """A tournament — a named roster of players tracked on one leaderboard.

    Configuration rather than polled data: a tournament's standings,
    matches, and live state are served under ``/v1/tournaments/{slug}/...``.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    leaderboard_id: int
    start_date: datetime | None
    end_date: datetime | None
    created_at: datetime


class TournamentUpdate(BaseModel):
    """Partial update for a tournament's metadata (``PATCH``).

    Every field is optional; only the fields present in the request body
    are applied. ``start_date`` / ``end_date`` may be set to ``null`` to
    clear the bound. ``name`` and ``leaderboard_id`` back non-nullable
    columns, so an explicit ``null`` for either is rejected with 422.

    ``slug`` is intentionally not updatable — it is the routing key
    consumers' URLs are built from.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    leaderboard_id: int | None = Field(default=None, gt=0)
    start_date: datetime | None = None
    end_date: datetime | None = None

    @field_validator("name", "leaderboard_id")
    @classmethod
    def _reject_explicit_null(cls, value: object) -> object:
        # A field-validator runs only for fields actually present in the
        # body, so a None reaching here is an explicit null for a column
        # that cannot store one — a 422, not a stored value.
        if value is None:
            raise ValueError("may not be null")
        return value

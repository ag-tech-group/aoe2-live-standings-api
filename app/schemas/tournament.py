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
    grand_finals_date: datetime | None
    created_at: datetime


class TournamentCreate(BaseModel):
    """Body for ``POST /v1/tournaments`` — create a new tournament.

    Required fields back non-nullable columns; the optional date fields
    behave the same way as on ``TournamentUpdate`` (omit to leave unset,
    explicit ``null`` is just an unset). ``slug`` is the routing key
    consumers' URLs are built from — restricted to lowercase alphanumeric
    + internal hyphens so the value drops into a path segment unchanged.
    """

    slug: str = Field(
        min_length=1,
        max_length=64,
        # No leading / trailing hyphen; internal hyphens (including consecutive) are fine.
        pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$",
    )
    name: str = Field(min_length=1, max_length=200)
    leaderboard_id: int = Field(gt=0)
    start_date: datetime | None = None
    grand_finals_date: datetime | None = None


class TournamentUpdate(BaseModel):
    """Partial update for a tournament's metadata (``PATCH``).

    Every field is optional; only the fields present in the request body
    are applied. ``start_date`` / ``grand_finals_date`` may be set to
    ``null`` to clear them. ``name`` and ``leaderboard_id`` back non-
    nullable columns, so an explicit ``null`` for either is rejected
    with 422.

    ``slug`` is intentionally not updatable — it is the routing key
    consumers' URLs are built from.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    leaderboard_id: int | None = Field(default=None, gt=0)
    start_date: datetime | None = None
    grand_finals_date: datetime | None = None

    @field_validator("name", "leaderboard_id")
    @classmethod
    def _reject_explicit_null(cls, value: object) -> object:
        # A field-validator runs only for fields actually present in the
        # body, so a None reaching here is an explicit null for a column
        # that cannot store one — a 422, not a stored value.
        if value is None:
            raise ValueError("may not be null")
        return value


class TournamentOwnerRead(BaseModel):
    """A criticalbit user authorized to manage a tournament.

    ``display_name``, ``email``, and ``avatar_url`` are resolved against
    criticalbit-auth-api at response time and cached briefly per ``user_id``.
    Any may be ``null``: ``display_name`` / ``avatar_url`` if the user has
    none set, ``email`` typically populated (auth-api always returns one,
    possibly a synthetic placeholder for Steam users who haven't gone
    through the accept-tos email gate). All three are null when the
    auth-api call fails — the row still resolves so the admin UI can
    degrade gracefully to just ``user_id``.
    """

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    created_at: datetime
    display_name: str | None = None
    email: str | None = None
    avatar_url: str | None = None


class TournamentOwnerCreate(BaseModel):
    """Body for ``POST /v1/tournaments/{slug}/owners`` — grant ownership.

    ``user_id`` is the target's criticalbit UUID (the ``sub`` claim from
    their access token). How a host discovers another user's UUID is
    out of scope for this API — typically copied from a user profile in
    the criticalbit ecosystem or shared directly.
    """

    user_id: str = Field(
        # The criticalbit-auth-api uses standard UUIDs for `sub`. Both
        # casings are tolerated; matching the column shape (String(36))
        # for length.
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
        min_length=36,
        max_length=36,
    )

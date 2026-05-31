"""Tournament request/response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

# A handful of URLs is plenty for a real host (typically one Twitch
# + one YouTube); the cap keeps the broadcast-live poller's quota
# footprint per tournament bounded.
_MAX_HOST_STREAM_URLS = 5
# Enough for typical Twitch / YouTube URLs (vanity + handle paths);
# keeps the JSON payload tight.
_MAX_HOST_STREAM_URL_LENGTH = 256


def _validated_host_stream_urls(urls: list[str]) -> list[str]:
    """Each entry must be a non-empty string within the length cap.

    No URL-syntax validation: the platform parsers in
    ``app.poller.broadcast`` already return ``None`` for non-twitch /
    non-youtube strings, so garbage is filtered downstream without
    ever costing a Helix call. Matches the posture on the roster's
    ``presentation.streamUrls``.
    """
    for url in urls:
        if not url:
            raise ValueError("host_stream_urls entries may not be empty")
        if len(url) > _MAX_HOST_STREAM_URL_LENGTH:
            raise ValueError(
                f"host_stream_urls entries must be at most {_MAX_HOST_STREAM_URL_LENGTH} chars"
            )
    return urls


class TournamentRead(BaseModel):
    """A tournament — a named roster of players tracked on one leaderboard.

    Configuration rather than polled data: a tournament's standings,
    matches, and live state are served under ``/v1/tournaments/{slug}/...``.
    The one exception is ``host_stream_live`` (#149) — a derived flag the
    router computes from the broadcast-live snapshot before serializing,
    so a host channel going live transitions the card within one poll
    cycle. The standings ``live`` SSE nudge already invalidates this query.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    leaderboard_id: int
    start_date: datetime | None
    grand_finals_date: datetime | None
    prize_pool_cents: int | None
    host_stream_urls: list[str]
    # Derived: any host channel currently broadcasting on any platform.
    # False when no host URLs are configured.
    host_stream_live: bool = False
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
    prize_pool_cents: int | None = Field(default=None, ge=0)
    host_stream_urls: list[str] = Field(
        default_factory=list,
        max_length=_MAX_HOST_STREAM_URLS,
    )

    @field_validator("host_stream_urls")
    @classmethod
    def _validate_host_stream_urls(cls, value: list[str]) -> list[str]:
        return _validated_host_stream_urls(value)


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
    prize_pool_cents: int | None = Field(default=None, ge=0)
    host_stream_urls: list[str] | None = Field(default=None, max_length=_MAX_HOST_STREAM_URLS)

    @field_validator("name", "leaderboard_id", "host_stream_urls")
    @classmethod
    def _reject_explicit_null(cls, value: object) -> object:
        # A field-validator runs only for fields actually present in the
        # body, so a None reaching here is an explicit null for a column
        # that cannot store one — a 422, not a stored value.
        if value is None:
            raise ValueError("may not be null")
        return value

    @field_validator("host_stream_urls")
    @classmethod
    def _validate_host_stream_urls(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        return _validated_host_stream_urls(value)


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

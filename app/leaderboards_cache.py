"""In-memory cache of leaderboard metadata.

The upstream ``getAvailableLeaderboards`` call returns a slowly-changing list
of leaderboards (id, name, ranked flag, matchtype mappings). The polling
worker (separate PR) will load this at startup and refresh on a daily
cadence; this module is the storage backend that worker writes to and the
``/v1/leaderboards`` route reads from.

For now — without the worker — the cache stays empty, so the endpoint
returns an empty list. Tests override the module-level dict directly via
``app.leaderboards_cache.set_cache(...)``.
"""

from __future__ import annotations

from datetime import datetime

from app.schemas.leaderboard import LeaderboardRead

# Sorted-by-id snapshot of the most recent upstream fetch. Kept as an
# immutable tuple so concurrent readers never see a partial update; the
# writer swaps the whole tuple atomically via `set_cache`.
_cache: tuple[LeaderboardRead, ...] = ()
_last_refreshed_at: datetime | None = None


def get_cache() -> tuple[LeaderboardRead, ...]:
    """Return the current snapshot. Empty tuple until the worker populates it."""
    return _cache


def get_last_refreshed_at() -> datetime | None:
    """Wall-clock time of the most recent successful upstream fetch, or None."""
    return _last_refreshed_at


def set_cache(items: list[LeaderboardRead], refreshed_at: datetime) -> None:
    """Replace the cached snapshot atomically. Called by the polling worker."""
    global _cache, _last_refreshed_at
    _cache = tuple(items)
    _last_refreshed_at = refreshed_at


def clear_cache() -> None:
    """Reset the cache. Used by tests between cases."""
    global _cache, _last_refreshed_at
    _cache = ()
    _last_refreshed_at = None

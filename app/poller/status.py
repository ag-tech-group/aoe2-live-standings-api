"""Per-poller last-successful-tick tracking (#9).

List endpoints answer "when was this data last refreshed?" via the
``last_polled_at`` field on the response envelope. Today that field is
the max ``updated_at`` across the returned rows, which goes ``null``
on an empty list — making it impossible for the frontend to
distinguish "the worker just checked and there's nothing" from "the
worker hasn't started yet". This module's tick timestamps fix that.

Each poller's ``tick_*`` function calls :func:`record_tick` after a
successful run. List endpoints pass their source to
``compute_last_polled_at`` (see ``app/schemas/common.py``); the
helper prefers the poller tick over the row-max, falling back to the
row-max only if the poller has never ticked in this process.

In-process state, single-instance per Cloud Run service. If the
backend ever scales to multiple worker instances or needs freshness
to survive process restarts, this moves to a ``poller_status`` table
(out of scope per #9).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum


class PollerSource(StrEnum):
    """Each list endpoint maps to one of these — the poller whose
    last-tick time best represents that endpoint's ``last_polled_at``."""

    PLAYER_STATS = "player_stats"
    RECENT_MATCHES = "recent_matches"
    LIVE_MATCHES = "live_matches"
    LEADERBOARDS = "leaderboards"


# Module-level state. Asyncio's single-threaded execution per loop
# makes this safe without a lock — a ``dict[]`` write or read is one
# bytecode in CPython.
_ticks: dict[PollerSource, datetime] = {}


def record_tick(source: PollerSource, *, when: datetime | None = None) -> None:
    """Record a successful tick for ``source``.

    Called from each poller's ``tick_*`` function after a successful
    run. Overwrites any prior tick — only the most recent matters.
    ``when`` defaults to ``datetime.now(UTC)``; passing it explicitly
    is useful in tests for deterministic timestamps.
    """
    _ticks[source] = when if when is not None else datetime.now(UTC)


def last_tick(source: PollerSource) -> datetime | None:
    """Return the most recent tick time for ``source``, or ``None`` if
    the poller has never ticked successfully in this process."""
    return _ticks.get(source)


def reset() -> None:
    """Clear all recorded ticks. Used by test fixtures."""
    _ticks.clear()

"""Shared schema types and helpers used across response models."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from pydantic import BaseModel

from app.poller.status import PollerSource, last_tick


class ListEnvelope[T](BaseModel):
    """Response shape for every list endpoint: ``{ last_polled_at, items }``.

    ``last_polled_at`` reports when the data was last refreshed against
    upstream. Per #9, it prefers the source poller's last successful
    tick time over the row-max ``updated_at`` — so an empty list (e.g.
    no live matches right now) still conveys "we checked recently"
    instead of returning ``null``.
    """

    last_polled_at: datetime | None
    items: list[T]


def compute_last_polled_at(
    timestamps: Iterable[datetime | None],
    *,
    source: PollerSource | None = None,
) -> datetime | None:
    """Return the freshness timestamp for a list response.

    Resolution order (per #9):

      1. ``source``'s last successful poller tick, if given and the
         poller has ticked at least once in this process.
      2. The max non-null timestamp across ``timestamps`` (the legacy
         row-``updated_at`` fallback, used during the brief window
         after process start before the first poller tick fires).
      3. ``None``.

    The poller tick takes priority because it answers "when did we
    last check upstream?" — which is the right freshness signal even
    when the rows themselves are unchanged or absent.
    """
    if source is not None:
        tick = last_tick(source)
        if tick is not None:
            return tick
    present = [t for t in timestamps if t is not None]
    return max(present) if present else None

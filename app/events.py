"""In-process pub/sub hub for Server-Sent Events.

The polling tasks publish a *nudge* after each successful DB commit; the
``GET /v1/stream`` SSE endpoint subscribes and relays nudges to connected
browsers. A nudge carries no data — just "this slice changed, refetch" —
so the REST endpoints stay the single source of truth.

Single-process only: the hub is a module-level singleton and fan-out
happens in one asyncio event loop. Multi-instance fan-out (Postgres
LISTEN/NOTIFY) is tracked in issue #14.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import structlog

logger = structlog.get_logger(__name__)

# A slow SSE client can't back the queue up indefinitely. Nudges are
# idempotent ("refetch now"), so once a queue is full we drop — the next
# nudge, or the client's own refetch, returns current state regardless of
# how many were missed. 16 is generous; a healthy client drains instantly.
_SUBSCRIBER_QUEUE_MAXSIZE = 16


class EventType(StrEnum):
    """SSE event names. Sent in the SSE ``event:`` field."""

    STANDINGS = "standings"
    LIVE = "live"
    MATCHES = "matches"


@dataclass(frozen=True)
class Nudge:
    """A single SSE nudge: which slice changed, and when it was polled."""

    event: EventType
    polled_at: datetime


class EventHub:
    """Fan-out hub. Each SSE connection subscribes with its own queue."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[Nudge]] = set()

    def subscribe(self) -> asyncio.Queue[Nudge]:
        """Register a new subscriber; returns the queue to read nudges from."""
        queue: asyncio.Queue[Nudge] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Nudge]) -> None:
        """Drop a subscriber (called when its SSE connection closes)."""
        self._subscribers.discard(queue)

    def publish(self, event: EventType) -> None:
        """Fan a nudge out to every current subscriber.

        Called by the polling tasks after a successful commit. Safe to
        call with zero subscribers (a no-op) — which is the common case
        in tests and whenever no browser is connected.
        """
        nudge = Nudge(event=event, polled_at=datetime.now(tz=UTC))
        for queue in self._subscribers:
            try:
                queue.put_nowait(nudge)
            except asyncio.QueueFull:
                # Slow consumer — drop. See _SUBSCRIBER_QUEUE_MAXSIZE.
                logger.warning("sse_nudge_dropped", event_type=event.value)

    @property
    def subscriber_count(self) -> int:
        """Number of currently-connected SSE subscribers."""
        return len(self._subscribers)


# Module-level singleton — imported by both the poller (publish) and the
# stream router (subscribe).
hub = EventHub()

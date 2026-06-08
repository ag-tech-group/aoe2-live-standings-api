"""SSE pub/sub, driven by polling a small ``nudge_versions`` table.

The polling worker bumps a per-event ``polled_at`` (``emit_nudge``) inside each
successful commit, so a rolled-back write never advances it. Every read-tier
instance runs a poll loop (``poll_for_nudges``) that reads that table through
its normal (pooled) engine and republishes changes to its local ``EventHub`` —
which fans out to the SSE clients connected to *that* instance.

This replaces the earlier Postgres ``LISTEN/NOTIFY`` spine (#196 Option B):
``LISTEN`` needs a session-pinned connection, which Managed Connection Pooling's
transaction mode can't provide. Polling a tiny table goes through the pooler
like any other query, so the api holds **no** direct/session-pinned connection
and ``num_backends`` no longer scales with instance count. The cost is up to
``_POLL_INTERVAL_SECONDS`` of nudge latency — imperceptible against the 15–60s
upstream poll cadence.

A nudge carries no domain data — just "this slice changed, refetch" — so the
REST endpoints stay the single source of truth.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.nudge import NudgeVersion

logger = structlog.get_logger(__name__)

# A slow SSE client can't back the queue up indefinitely. Nudges are
# idempotent ("refetch now"), so once a queue is full we drop — the next
# nudge, or the client's own refetch, returns current state regardless of
# how many were missed. 16 is generous; a healthy client drains instantly.
_SUBSCRIBER_QUEUE_MAXSIZE = 16

# How often each instance polls ``nudge_versions`` for changes. 2s keeps nudge
# latency well under the 15s live-match cadence while costing only a tiny
# indexed-PK scan per instance every couple seconds (≈0.5 qps/instance).
_POLL_INTERVAL_SECONDS = 2

# Cadence for sampling the live SSE subscriber count into the logs, where
# the `sse_subscriber_count` log-based metric turns it into a Cloud
# Monitoring series. 30s matches the standings poll cadence — fine enough
# to watch seats fill during a live match without flooding the logs.
_SUBSCRIBER_SAMPLE_SECONDS = 30


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
    """Per-instance fan-out hub. Each SSE connection subscribes with its own queue."""

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

    def publish(self, event: EventType, polled_at: datetime | None = None) -> None:
        """Fan a nudge out to every current subscriber.

        Called by the poll loop for each event whose ``polled_at`` advanced.
        ``polled_at`` is the timestamp the worker stamped; if omitted (e.g. in
        tests that call ``publish`` directly to exercise the hub in isolation),
        falls back to ``now()``.
        """
        nudge = Nudge(event=event, polled_at=polled_at or datetime.now(tz=UTC))
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


# Module-level singleton — imported by the stream router (subscribe) and
# by the poll loop (publish).
hub = EventHub()


async def sample_subscriber_count(event_hub: EventHub) -> None:
    """Periodically log the live SSE subscriber count for Cloud Monitoring.

    ``event_hub`` already knows how many SSE connections are open on this
    instance; this samples that on a fixed cadence and emits it as a
    structured ``sse_subscriber_count`` event. The ``sse_subscriber_count``
    log-based metric (``infra/terraform/monitoring.tf``) extracts ``count``
    into a per-instance Cloud Monitoring series — the only direct read on
    SSE-seat demand, which is the api tier's binding resource (#194).

    Runs on the read tier (``listener_enabled``) beside the poll loop; the
    ``asyncio.sleep`` surfaces ``CancelledError`` at lifespan shutdown so the
    task exits cleanly.
    """
    while True:
        await asyncio.sleep(_SUBSCRIBER_SAMPLE_SECONDS)
        logger.info("sse_subscriber_count", count=event_hub.subscriber_count)


async def emit_nudge(session: AsyncSession, event: EventType) -> None:
    """Advance this event's nudge version inside the session's transaction.

    Bumps ``nudge_versions.polled_at`` to ``now()`` for ``event``; the write is
    part of the caller's transaction, so a rolled-back transaction never
    advances it — "data changed" is signalled iff the data actually changed.
    Each api instance's ``poll_for_nudges`` turns the advance into an SSE nudge.
    Read-modify-write works on every dialect (the unit tests' SQLite included);
    the worker is a singleton emitting one row per event type, so there's no
    same-row contention to need a dialect-specific UPSERT.
    """
    now = datetime.now(tz=UTC)
    row = await session.get(NudgeVersion, event.value)
    if row is None:
        session.add(NudgeVersion(event=event.value, polled_at=now))
    else:
        row.polled_at = now


async def poll_for_nudges(session_maker: async_sessionmaker[AsyncSession]) -> None:
    """Poll ``nudge_versions`` and drive the local hub when an event advances.

    Replaces the LISTEN/NOTIFY listener. Uses a short-lived session per poll so
    it holds no connection between polls — fully pooler-friendly. On the first
    poll it records the current versions WITHOUT publishing (a freshly-connected
    SSE client already has current data from its REST fetch; nudges are for
    *subsequent* changes). Cancelled at lifespan shutdown.
    """
    last_seen: dict[str, datetime] = {}
    initialized = False
    logger.info("nudge_poller_started", interval_seconds=_POLL_INTERVAL_SECONDS)
    while True:
        try:
            async with session_maker() as session:
                rows = (await session.execute(select(NudgeVersion))).scalars().all()
            for row in rows:
                previous = last_seen.get(row.event)
                last_seen[row.event] = row.polled_at
                if not initialized:
                    continue
                if previous is None or row.polled_at > previous:
                    try:
                        hub.publish(EventType(row.event), polled_at=row.polled_at)
                    except ValueError:
                        logger.warning("nudge_unknown_event", event=row.event)
            initialized = True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # A transient DB blip shouldn't kill the loop; log and retry next tick.
            logger.error("nudge_poll_failed", error=str(e))
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)

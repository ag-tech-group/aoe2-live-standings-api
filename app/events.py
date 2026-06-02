"""SSE pub/sub, driven by Postgres ``LISTEN/NOTIFY``.

The polling worker emits a NOTIFY on the ``aoe2_events`` channel after
each successful commit (``emit_nudge`` inside the transaction, so a
rolled-back write never fires a nudge). Every read-tier instance runs
a long-lived LISTEN connection (``listen_for_nudges``) that receives
those payloads and republishes them to its local ``EventHub`` — which
fans out to the SSE clients connected to *that* instance.

A nudge carries no domain data — just "this slice changed, refetch" —
so the REST endpoints stay the single source of truth.

The LISTEN/NOTIFY layer is the cross-instance pub/sub issue #14 calls
for; in Phase 2 of #14 the listener runs in the same process as the
pollers, and Phase 3 splits them into separate Cloud Run services.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import asyncpg
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

# A slow SSE client can't back the queue up indefinitely. Nudges are
# idempotent ("refetch now"), so once a queue is full we drop — the next
# nudge, or the client's own refetch, returns current state regardless of
# how many were missed. 16 is generous; a healthy client drains instantly.
_SUBSCRIBER_QUEUE_MAXSIZE = 16

# Single NOTIFY channel for every nudge type; the JSON payload carries
# ``event`` (one of EventType) and ``polled_at``. Keeping it to one
# channel means one LISTEN connection per read-tier instance, regardless
# of how many event types we add.
_CHANNEL = "aoe2_events"

# Liveness ping cadence on the LISTEN connection. NOTIFY delivery alone
# can't tell us the connection has died (asyncpg's add_listener is
# passive), so we ping periodically and reconnect on failure.
_LISTENER_PING_SECONDS = 30

# Backoff between reconnect attempts when the LISTEN connection drops.
# Long enough not to hammer the DB during a restart; short enough that
# nudges resume quickly once it recovers.
_LISTENER_RECONNECT_SECONDS = 5

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

        Called by the NOTIFY listener for each incoming nudge from the DB.
        ``polled_at`` is the timestamp the poller stamped on the payload;
        if omitted (e.g. in tests that call ``publish`` directly to
        exercise the hub in isolation), falls back to ``now()``.
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
# by the NOTIFY listener (publish).
hub = EventHub()


async def sample_subscriber_count(event_hub: EventHub) -> None:
    """Periodically log the live SSE subscriber count for Cloud Monitoring.

    ``event_hub`` already knows how many SSE connections are open on this
    instance; this samples that on a fixed cadence and emits it as a
    structured ``sse_subscriber_count`` event. The ``sse_subscriber_count``
    log-based metric (``infra/terraform/monitoring.tf``) extracts ``count``
    into a per-instance Cloud Monitoring series — the only direct read on
    SSE-seat demand, which is the api tier's binding resource (#194).

    Runs on the read tier (``listener_enabled``) beside the NOTIFY listener;
    the ``asyncio.sleep`` surfaces ``CancelledError`` at lifespan shutdown so
    the task exits cleanly.
    """
    while True:
        await asyncio.sleep(_SUBSCRIBER_SAMPLE_SECONDS)
        logger.info("sse_subscriber_count", count=event_hub.subscriber_count)


async def emit_nudge(session: AsyncSession, event: EventType) -> None:
    """Queue a Postgres NOTIFY inside the session's transaction.

    The NOTIFY is held until ``COMMIT`` succeeds — a rolled-back
    transaction never fires the nudge, so "data changed" is signalled iff
    the data actually changed. No-op on non-PostgreSQL dialects (the
    SQLite used by the unit tests has no ``LISTEN/NOTIFY``).
    """
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    payload = json.dumps({"event": event.value, "polled_at": datetime.now(tz=UTC).isoformat()})
    await session.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": _CHANNEL, "payload": payload},
    )


def _asyncpg_dsn(database_url: str) -> str:
    """Strip SQLAlchemy's ``+asyncpg`` driver marker for a plain asyncpg DSN."""
    return database_url.replace("+asyncpg", "")


def _on_notify(_connection, _pid, _channel, payload: str) -> None:
    """asyncpg LISTEN callback: parse the payload and drive the local hub."""
    try:
        data = json.loads(payload)
        hub.publish(
            EventType(data["event"]),
            polled_at=datetime.fromisoformat(data["polled_at"]),
        )
    except (KeyError, ValueError) as e:
        logger.warning("nudge_parse_failed", error=str(e), payload=payload)


async def listen_for_nudges(database_url: str) -> None:
    """Long-running LISTEN task: drive the local hub from incoming NOTIFY.

    One dedicated asyncpg connection per process. A periodic ping detects
    a dropped connection (asyncpg's ``add_listener`` is passive, so we
    can't otherwise tell when the wire goes silent); on failure, the loop
    reconnects after ``_LISTENER_RECONNECT_SECONDS``. The task is
    cancelled at lifespan shutdown.
    """
    dsn = _asyncpg_dsn(database_url)
    while True:
        try:
            conn = await asyncpg.connect(dsn=dsn)
            try:
                await conn.add_listener(_CHANNEL, _on_notify)
                logger.info("nudge_listener_started", channel=_CHANNEL)
                while True:
                    await asyncio.sleep(_LISTENER_PING_SECONDS)
                    await conn.fetchval("SELECT 1")
            finally:
                await conn.close()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("nudge_listener_failed", error=str(e))
            await asyncio.sleep(_LISTENER_RECONNECT_SECONDS)

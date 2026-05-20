"""GET /v1/stream — Server-Sent Events nudge stream.

Emits a nudge (``event:`` + a tiny ``data:`` payload) whenever a polling
task commits fresh data, plus a heartbeat comment every
``_HEARTBEAT_INTERVAL_SECONDS`` so proxies don't drop an idle connection.

Consumers react to a nudge by refetching the matching REST endpoint —
the nudge itself carries no domain data. See ``app.events`` for the hub.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.events import Nudge, hub

router = APIRouter(tags=["stream"])

# Heartbeat cadence. Comfortably under typical proxy/load-balancer idle
# timeouts (60s+) so a quiet stream stays open between poll cycles.
_HEARTBEAT_INTERVAL_SECONDS = 20

# SSE comment line — EventSource ignores it; it exists only to keep the
# TCP connection and any intermediary proxies from going idle.
_HEARTBEAT = ": heartbeat\n\n"


def _format_nudge(nudge: Nudge) -> str:
    """Render a Nudge as an SSE event frame."""
    payload = json.dumps({"polled_at": nudge.polled_at.isoformat()})
    return f"event: {nudge.event.value}\ndata: {payload}\n\n"


async def _event_stream(request: Request) -> AsyncIterator[str]:
    """Yield SSE frames for one connected client until it disconnects."""
    queue = hub.subscribe()
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                nudge = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_INTERVAL_SECONDS)
            except TimeoutError:
                # No nudge this interval — emit a heartbeat to keep the
                # connection (and intermediary proxies) alive.
                yield _HEARTBEAT
                continue
            yield _format_nudge(nudge)
    finally:
        # Runs on client disconnect (the generator is cancelled) as well
        # as the clean break above — either way, drop the subscriber so
        # the hub doesn't fan out to a dead queue.
        hub.unsubscribe(queue)


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    """Open a Server-Sent Events stream of nudges.

    Long-lived: the connection stays open until the client disconnects or
    Cloud Run's request timeout recycles it (~hourly), at which point the
    browser's ``EventSource`` reconnects transparently.
    """
    return StreamingResponse(
        _event_stream(request),
        media_type="text/event-stream",
        headers={
            # Never cache a stream; never let a proxy buffer it (events
            # must flush to the client the instant they're written).
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )

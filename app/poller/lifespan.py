"""FastAPI ``lifespan`` integration for the listener + poller background tasks.

The same image runs as two Cloud Run services in production (issue #14):

- The **API** service has ``LISTENER_ENABLED=true`` and ``POLLING_ENABLED=false``.
  Its lifespan starts only the LISTEN/NOTIFY listener, which republishes
  nudges from the DB to the local ``EventHub`` for SSE fan-out.
- The **worker** service has ``LISTENER_ENABLED=false`` and ``POLLING_ENABLED=true``.
  Its lifespan loads leaderboards, seeds a tournament if needed, and
  starts the three long-running polling tasks (30s / 60s / 15s).

In local dev both flags default true, so a single uvicorn process runs
everything — mono mode. In tests, ``ASGITransport`` bypasses the lifespan
entirely so neither task group ever starts.

On shutdown the lifespan cancels every task it started, awaits the
unwinding, then closes the shared upstream client (if one was built).
``asyncio.CancelledError`` surfaces inside each ``while True`` loop; the
runners re-raise it deliberately so the loop exits cleanly rather than
swallowing the signal in their per-tick try/except.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.config import settings
from app.database import async_session_maker
from app.events import listen_for_nudges
from app.poller.broadcast import (
    TwitchLiveClient,
    YouTubeLiveClient,
    build_broadcast_http_client,
)
from app.poller.client import build_upstream_client
from app.poller.leaderboards import load_leaderboards
from app.poller.live_matches import run_live_matches_poller
from app.poller.live_streams import run_twitch_live_poller, run_youtube_live_poller
from app.poller.player_stats import run_player_stats_poller
from app.poller.recent_matches import run_recent_matches_poller

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Boot the listener and/or pollers per settings; tear them down on shutdown."""
    tasks: list[asyncio.Task] = []
    client = None
    broadcast_http = None

    if settings.listener_enabled:
        tasks.append(
            asyncio.create_task(
                listen_for_nudges(settings.database_url),
                name="listen_nudges",
            )
        )
        logger.info("listener_starting")

    if settings.polling_enabled:
        client = build_upstream_client()
        matchtype_map = await load_leaderboards(client, async_session_maker)
        tasks.extend(
            [
                asyncio.create_task(
                    run_player_stats_poller(client, async_session_maker),
                    name="poll_player_stats",
                ),
                asyncio.create_task(
                    run_recent_matches_poller(client, async_session_maker, matchtype_map),
                    name="poll_recent_matches",
                ),
                asyncio.create_task(
                    run_live_matches_poller(client, async_session_maker),
                    name="poll_live_matches",
                ),
            ]
        )
        # Broadcast-live detection (#112) is opt-in per platform: each task
        # starts only when its credentials are set, so a deploy without them
        # (and local dev) simply runs no stream detection. Twitch and YouTube
        # share one httpx client, built lazily on first use.
        if settings.twitch_client_id and settings.twitch_client_secret:
            broadcast_http = build_broadcast_http_client()
            twitch = TwitchLiveClient(
                settings.twitch_client_id, settings.twitch_client_secret, broadcast_http
            )
            tasks.append(
                asyncio.create_task(
                    run_twitch_live_poller(twitch, async_session_maker),
                    name="poll_twitch_live",
                )
            )
        if settings.youtube_api_key:
            broadcast_http = broadcast_http or build_broadcast_http_client()
            youtube = YouTubeLiveClient(settings.youtube_api_key, broadcast_http)
            tasks.append(
                asyncio.create_task(
                    run_youtube_live_poller(youtube, async_session_maker),
                    name="poll_youtube_live",
                )
            )
        logger.info("polling_starting")

    if not tasks:
        logger.info("lifespan_idle")
        yield
        return

    try:
        yield
    finally:
        logger.info("lifespan_stopping")
        for task in tasks:
            task.cancel()
        # `return_exceptions=True` so the CancelledError each runner
        # re-raises doesn't propagate out of shutdown.
        await asyncio.gather(*tasks, return_exceptions=True)
        if client is not None:
            await client.aclose()
        if broadcast_http is not None:
            await broadcast_http.aclose()

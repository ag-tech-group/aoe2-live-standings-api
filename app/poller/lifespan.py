"""FastAPI ``lifespan`` integration for the listener + poller background tasks.

The same image runs as two Cloud Run services in production (issue #14):

- The **API** service has ``LISTENER_ENABLED=true`` and ``POLLING_ENABLED=false``.
  Its lifespan starts only the LISTEN/NOTIFY listener, which republishes
  nudges from the DB to the local ``EventHub`` for SSE fan-out.
- The **worker** service has ``LISTENER_ENABLED=false`` and ``POLLING_ENABLED=true``.
  Its lifespan starts the leaderboards loader and the long-running polling
  tasks (30s / 60s / 15s) — all background tasks, so startup never blocks on
  the DB or upstream (#177).

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
from app.events import hub, listen_for_nudges, sample_subscriber_count
from app.poller.broadcast import (
    TwitchLiveClient,
    YouTubeLiveClient,
    build_broadcast_http_client,
)
from app.poller.client import build_upstream_client
from app.poller.leaderboards import run_leaderboards_loader
from app.poller.live_matches import run_live_matches_poller
from app.poller.live_streams import run_twitch_live_poller, run_youtube_live_poller
from app.poller.parsers import DEFAULT_MATCHTYPE_TO_LEADERBOARD
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
        # Sample the SSE subscriber count on the read tier — these instances
        # host the hub the listener fans nudges out to, so this is where the
        # live-seat number lives (#194).
        tasks.append(
            asyncio.create_task(
                sample_subscriber_count(hub),
                name="sample_sse_subscribers",
            )
        )
        logger.info("listener_starting")

    if settings.polling_enabled:
        client = build_upstream_client()
        # Seed the matchtype->leaderboard map with the static floor and let the
        # loader enrich/refresh it from upstream in the background. This must
        # NOT block startup on a DB/upstream call: the worker has to bind its
        # port to pass Cloud Run's health check, and a degraded DB at startup
        # would otherwise wedge the whole deploy (#177). The recent-matches
        # poller reads this same dict each cycle, so in-place updates from the
        # loader land without a restart.
        matchtype_map: dict[int, int] = dict(DEFAULT_MATCHTYPE_TO_LEADERBOARD)
        tasks.extend(
            [
                asyncio.create_task(
                    run_leaderboards_loader(client, async_session_maker, matchtype_map),
                    name="load_leaderboards",
                ),
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

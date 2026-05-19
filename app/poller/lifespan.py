"""FastAPI ``lifespan`` integration for the polling worker.

On startup: build one shared ``httpx.AsyncClient``, load leaderboard
metadata into the in-memory cache, and start three long-running
``asyncio.Task``s — one per polling cadence (30s / 60s / 15s).

On shutdown: cancel the tasks, await their unwinding, then close the
shared client. Cancellation surfaces as ``CancelledError`` inside each
``while True`` loop; the runners re-raise it deliberately so the loop
exits cleanly rather than swallowing the signal in their per-tick
try/except.

The whole lifespan is gated by ``settings.polling_enabled``. When
disabled, the app boots normally with an empty leaderboard cache and no
background tasks — useful for local dev that doesn't want to hit
upstream, and a defensive default for test environments that bypass
``ASGITransport``'s lifespan-off behavior.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.config import settings
from app.database import async_session_maker
from app.poller.client import build_upstream_client
from app.poller.leaderboards import load_leaderboards
from app.poller.live_matches import run_live_matches_poller
from app.poller.player_stats import run_player_stats_poller
from app.poller.recent_matches import run_recent_matches_poller

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Boot the polling worker on app startup; tear it down on shutdown."""
    if not settings.polling_enabled:
        logger.info("polling_disabled")
        yield
        return

    profile_ids = settings.tracked_profile_id_list
    logger.info("polling_starting", tracked_profile_count=len(profile_ids))

    client = build_upstream_client()
    matchtype_map = await load_leaderboards(client)

    tasks = [
        asyncio.create_task(
            run_player_stats_poller(client, profile_ids, async_session_maker),
            name="poll_player_stats",
        ),
        asyncio.create_task(
            run_recent_matches_poller(client, profile_ids, async_session_maker, matchtype_map),
            name="poll_recent_matches",
        ),
        asyncio.create_task(
            run_live_matches_poller(client, profile_ids, async_session_maker),
            name="poll_live_matches",
        ),
    ]

    try:
        yield
    finally:
        logger.info("polling_stopping")
        for task in tasks:
            task.cancel()
        # Wait for each task to actually finish unwinding. `gather` with
        # `return_exceptions=True` swallows the CancelledError each task
        # re-raises so we don't propagate it out of shutdown.
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.aclose()

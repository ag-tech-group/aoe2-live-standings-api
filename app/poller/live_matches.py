"""``poll_live_matches``: 15s cadence, one upstream call per cycle.

``findAdvertisements`` returns every open lobby on the upstream — there's
no profile filter on the request side. We pull the full list, filter to
lobbies whose ``matchmembers`` include a tracked profile, and upsert
those rows with the live-specific helper (which preserves
``completed_at`` so a stale advertisement can never roll back a match
the recent-matches feed already marked complete).
"""

from __future__ import annotations

import asyncio

import httpx
import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.poller.parsers import parse_live_advertisements
from app.poller.upserts import upsert_match_from_live

logger = structlog.get_logger(__name__)

_ENDPOINT = "/community/advertisement/findAdvertisements"
_DEFAULT_INTERVAL_SECONDS = 15


async def tick_live_matches(
    client: httpx.AsyncClient,
    profile_ids: list[int],
    session_maker: async_sessionmaker,
) -> None:
    """One cycle. Skip the upstream hit entirely if nothing is tracked."""
    if not profile_ids:
        return

    response = await client.get(_ENDPOINT, params={"title": "age2"})
    response.raise_for_status()
    matches = parse_live_advertisements(response.json(), set(profile_ids))

    async with session_maker() as session:
        for match in matches:
            await upsert_match_from_live(session, match)
        await session.commit()
    logger.info("poll_live_matches_ok", matches=len(matches))


async def run_live_matches_poller(
    client: httpx.AsyncClient,
    profile_ids: list[int],
    session_maker: async_sessionmaker,
    interval_seconds: int = _DEFAULT_INTERVAL_SECONDS,
) -> None:
    """Long-running task — polls every ``interval_seconds``."""
    while True:
        try:
            await tick_live_matches(client, profile_ids, session_maker)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("poll_live_matches_failed", error=str(e))
        await asyncio.sleep(interval_seconds)

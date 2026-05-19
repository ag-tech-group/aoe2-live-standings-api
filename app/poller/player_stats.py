"""``poll_player_stats``: 30s cadence, one batched upstream call.

One ``GetPersonalStat`` request carries every tracked profile in a single
batch (verified up to 32 in the data-source spike). For tournament-scale
deployments this stays a single HTTP request per cycle regardless of
roster size, well below the upstream's empirical rate-limit headroom.
"""

from __future__ import annotations

import asyncio

import httpx
import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.poller.parsers import parse_player_stats
from app.poller.upserts import upsert_player, upsert_player_rating

logger = structlog.get_logger(__name__)

_ENDPOINT = "/community/leaderboard/GetPersonalStat"
_DEFAULT_INTERVAL_SECONDS = 30


def _format_profile_ids(profile_ids: list[int]) -> str:
    """Render the profile-ID list in the bracketed shape Relic expects.

    Upstream parses ``profile_ids=[1,2,3]`` (URL-encoded but literally
    bracketed inside) — not a repeated query parameter and not a JSON
    object outside the brackets.
    """
    return "[" + ",".join(str(p) for p in profile_ids) + "]"


async def tick_player_stats(
    client: httpx.AsyncClient,
    profile_ids: list[int],
    session_maker: async_sessionmaker,
) -> None:
    """One fetch + parse + upsert cycle. No-op if nothing is tracked."""
    if not profile_ids:
        return
    response = await client.get(
        _ENDPOINT,
        params={"title": "age2", "profile_ids": _format_profile_ids(profile_ids)},
    )
    response.raise_for_status()
    players, ratings = parse_player_stats(response.json())

    async with session_maker() as session:
        for row in players:
            await upsert_player(session, row)
        for row in ratings:
            await upsert_player_rating(session, row)
        await session.commit()
    logger.info("poll_player_stats_ok", players=len(players), ratings=len(ratings))


async def run_player_stats_poller(
    client: httpx.AsyncClient,
    profile_ids: list[int],
    session_maker: async_sessionmaker,
    interval_seconds: int = _DEFAULT_INTERVAL_SECONDS,
) -> None:
    """Long-running task — ticks forever, swallows per-cycle errors."""
    while True:
        try:
            await tick_player_stats(client, profile_ids, session_maker)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("poll_player_stats_failed", error=str(e))
        await asyncio.sleep(interval_seconds)

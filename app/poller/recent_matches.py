"""``poll_recent_matches``: 60s cycle, fans out one upstream call per profile.

Unlike player stats — which Relic accepts as a single batched call —
``getRecentMatchHistory`` returns per-profile recent history. We fan
out one call per tracked profile, capped at 4 concurrent in-flight
requests so a 32-player roster doesn't hit the upstream as 32 parallel
sockets (still well within observed limits, but a polite default).

When the same match is returned for two tracked players (they faced
each other), the duplicate upserts are absorbed by ``ON CONFLICT`` —
no dedupe needed in Python.
"""

from __future__ import annotations

import asyncio

import httpx
import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.events import EventType, hub
from app.poller.parsers import parse_recent_matches
from app.poller.upserts import upsert_match_from_recent, upsert_match_player

logger = structlog.get_logger(__name__)

_ENDPOINT = "/community/leaderboard/getRecentMatchHistory"
_DEFAULT_INTERVAL_SECONDS = 60
_DEFAULT_CONCURRENCY = 4


async def _fetch_one(
    client: httpx.AsyncClient,
    profile_id: int,
    semaphore: asyncio.Semaphore,
) -> dict | None:
    """Fetch one profile's recent matches under the concurrency gate."""
    async with semaphore:
        response = await client.get(
            _ENDPOINT,
            params={"title": "age2", "profile_ids": f"[{profile_id}]"},
        )
        response.raise_for_status()
        return response.json()


async def tick_recent_matches(
    client: httpx.AsyncClient,
    profile_ids: list[int],
    session_maker: async_sessionmaker,
    matchtype_to_leaderboard: dict[int, int],
    concurrency: int = _DEFAULT_CONCURRENCY,
) -> None:
    """One fan-out cycle: fetch each tracked profile's recent matches and upsert."""
    if not profile_ids:
        return

    semaphore = asyncio.Semaphore(concurrency)
    # `return_exceptions=True` so one upstream failure doesn't take down
    # the whole batch — each failed profile gets logged and skipped while
    # the rest land in the DB.
    results = await asyncio.gather(
        *(_fetch_one(client, p, semaphore) for p in profile_ids),
        return_exceptions=True,
    )

    total_matches = 0
    total_players = 0
    async with session_maker() as session:
        for profile_id, result in zip(profile_ids, results, strict=True):
            if isinstance(result, Exception):
                logger.error(
                    "recent_matches_fetch_failed",
                    profile_id=profile_id,
                    error=str(result),
                )
                continue
            matches, players = parse_recent_matches(result, matchtype_to_leaderboard)
            for match in matches:
                await upsert_match_from_recent(session, match)
            for mp in players:
                await upsert_match_player(session, mp)
            total_matches += len(matches)
            total_players += len(players)
        await session.commit()
    logger.info(
        "poll_recent_matches_ok",
        profiles=len(profile_ids),
        matches=total_matches,
        players=total_players,
    )
    hub.publish(EventType.MATCHES)


async def run_recent_matches_poller(
    client: httpx.AsyncClient,
    profile_ids: list[int],
    session_maker: async_sessionmaker,
    matchtype_to_leaderboard: dict[int, int],
    interval_seconds: int = _DEFAULT_INTERVAL_SECONDS,
    concurrency: int = _DEFAULT_CONCURRENCY,
) -> None:
    """Long-running task — fans out every ``interval_seconds``."""
    while True:
        try:
            await tick_recent_matches(
                client,
                profile_ids,
                session_maker,
                matchtype_to_leaderboard,
                concurrency,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("poll_recent_matches_failed", error=str(e))
        await asyncio.sleep(interval_seconds)

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
from collections import Counter

import httpx
import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.events import EventType, emit_nudge
from app.poller.parsers import parse_recent_matches
from app.poller.roster import get_tracked_profile_ids
from app.poller.upserts import (
    upsert_match_from_recent,
    upsert_match_player,
    upsert_profile_alias,
)

logger = structlog.get_logger(__name__)

_ENDPOINT = "/community/leaderboard/getRecentMatchHistory"
_DEFAULT_INTERVAL_SECONDS = 60
_DEFAULT_CONCURRENCY = 4
# Cap the per-profile id list on the aggregated failure log so a large
# roster failing wholesale (an outage) doesn't dump hundreds of ids.
_MAX_LOGGED_FAILED_PROFILE_IDS = 10


def _failure_key(exc: Exception) -> int | str:
    """Bucket a fetch failure by upstream HTTP status, else exception type."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    return type(exc).__name__


def _log_fetch_failures(failures: list[tuple[int, Exception]], total: int) -> None:
    """Emit one aggregated error for a cycle's per-profile fetch failures.

    This is a fan-out task (one request per profile), so a worldsedgelink
    outage fails every profile at once. Logging per profile turned a single
    outage into N near-identical ERROR lines — N Sentry issues (the
    profile_id rides in the payload) and N events against the un-sampled
    error quota (the 2026-06-09 502 storm). Folding them into one event per
    cycle keeps the diagnostic split — the status breakdown plus a capped id
    sample separates "upstream is down" (``{502: 18}``) from "one profile is
    stale" (``{404: 1}``) — at a fraction of the volume. The Sentry-side
    fingerprint in ``app/sentry.py`` then collapses these across all poll
    tasks into one issue.
    """
    statuses = Counter(_failure_key(exc) for _, exc in failures)
    logger.error(
        "recent_matches_fetch_failed",
        failed=len(failures),
        total=total,
        statuses=dict(statuses),
        profile_ids=[pid for pid, _ in failures[:_MAX_LOGGED_FAILED_PROFILE_IDS]],
        sample_error=str(failures[0][1]),
    )


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
    # Collected and logged once after the loop — see _log_fetch_failures.
    failures: list[tuple[int, Exception]] = []
    async with session_maker() as session:
        for profile_id, result in zip(profile_ids, results, strict=True):
            if isinstance(result, Exception):
                failures.append((profile_id, result))
                continue
            matches, players, aliases = parse_recent_matches(result, matchtype_to_leaderboard)
            for match in matches:
                await upsert_match_from_recent(session, match)
            for mp in players:
                await upsert_match_player(session, mp)
            # Names for everyone in these matches — incl. untracked opponents —
            # so the recent-games hint can label them (#349).
            for alias_row in aliases:
                await upsert_profile_alias(session, alias_row)
            total_matches += len(matches)
            total_players += len(players)
        # Match writes drive the recent-results display — emit a NOTIFY so
        # SSE subscribers on every read-tier instance get a refetch nudge.
        await emit_nudge(session, EventType.MATCHES)
        await session.commit()
    if failures:
        _log_fetch_failures(failures, total=len(profile_ids))
    logger.info(
        "poll_recent_matches_ok",
        profiles=len(profile_ids),
        matches=total_matches,
        players=total_players,
    )


async def run_recent_matches_poller(
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker,
    matchtype_to_leaderboard: dict[int, int],
    interval_seconds: int = _DEFAULT_INTERVAL_SECONDS,
    concurrency: int = _DEFAULT_CONCURRENCY,
) -> None:
    """Long-running task — fans out every ``interval_seconds``.

    The tracked roster is re-resolved from the database each cycle, so
    roster edits made through the management API are picked up without a
    redeploy.
    """
    while True:
        try:
            async with session_maker() as session:
                profile_ids = await get_tracked_profile_ids(session)
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

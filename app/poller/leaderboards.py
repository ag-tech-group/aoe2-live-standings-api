"""Leaderboard metadata loader.

``getAvailableLeaderboards`` rarely changes (new ladders land when Relic
publishes them). ``load_leaderboards`` does one fetch + upsert and returns the
``matchtype_id -> leaderboard_id`` map the recent-matches poller needs;
``run_leaderboards_loader`` runs it in the background on a retry/refresh loop so
worker startup never blocks on the DB or upstream (#177), and a newly published
leaderboard is picked up on the next refresh rather than only on restart.
"""

from __future__ import annotations

import asyncio

import httpx
import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.poller.parsers import (
    DEFAULT_MATCHTYPE_TO_LEADERBOARD,
    matchtype_to_leaderboard_map,
    parse_available_leaderboards,
)
from app.poller.upserts import upsert_leaderboard

logger = structlog.get_logger(__name__)

_ENDPOINT = "/community/leaderboard/getAvailableLeaderboards"

# Background loader cadence: retry quickly until upstream returns real
# matchtypes (more than the static floor), then refresh slowly.
_DEFAULT_RETRY_SECONDS = 60
_DEFAULT_REFRESH_SECONDS = 1800


async def load_leaderboards(
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker,
) -> dict[int, int]:
    """Fetch leaderboards, upsert each into the DB, return the matchtype mapping.

    The returned ``{matchtype_id: leaderboard_id}`` map is consumed by the
    recent-matches poller to fill ``Match.leaderboard_id`` without a second
    upstream call per match. The upstream map is merged *over*
    ``DEFAULT_MATCHTYPE_TO_LEADERBOARD`` so the core ranked ladder is always
    mapped even when upstream omits its ``matchtypes`` (see that constant for
    the 2026-06-01 incident this guards against). On hard failure the table
    stays unchanged, ``/v1/leaderboards`` returns the last successful snapshot,
    and the static floor is returned so the worker can still tag core-ladder
    matches.
    """
    try:
        response = await client.get(_ENDPOINT, params={"title": "age2"})
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.error("load_leaderboards_failed", error=str(e))
        return dict(DEFAULT_MATCHTYPE_TO_LEADERBOARD)

    rows = parse_available_leaderboards(payload)
    async with session_maker() as session:
        for row in rows:
            await upsert_leaderboard(session, row)
        await session.commit()

    upstream_map = matchtype_to_leaderboard_map(payload)
    if not upstream_map:
        # Leaderboards present but no matchtypes — the silent failure that
        # emptied tournament standings on 2026-06-01. Log loudly; the static
        # floor below keeps the core ladder tagged until upstream recovers.
        logger.warning("load_leaderboards_no_matchtypes", leaderboards=len(rows))
    mapping = {**DEFAULT_MATCHTYPE_TO_LEADERBOARD, **upstream_map}
    logger.info("load_leaderboards_ok", count=len(rows), matchtypes=len(mapping))
    return mapping


async def run_leaderboards_loader(
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker,
    matchtype_map: dict[int, int],
    retry_seconds: int = _DEFAULT_RETRY_SECONDS,
    refresh_seconds: int = _DEFAULT_REFRESH_SECONDS,
) -> None:
    """Keep ``matchtype_map`` populated from upstream without blocking startup.

    Mutates the caller's ``matchtype_map`` in place, so the recent-matches
    poller — which reads the same dict each cycle — picks up new mappings with
    no restart. The caller seeds the map with ``DEFAULT_MATCHTYPE_TO_LEADERBOARD``
    and it never regresses below that floor: ``load_leaderboards`` already merges
    the floor, and a failed load leaves the current contents untouched.

    Retries on the short ``retry_seconds`` cadence until upstream returns real
    matchtypes (more than the floor), then settles into the slow
    ``refresh_seconds`` refresh. Re-raises ``CancelledError`` so lifespan
    shutdown unwinds cleanly; any other error (e.g. a DB upsert failing under
    connection saturation) is logged and retried rather than killing the task —
    that resilience is the point of #177: a degraded DB at startup can no longer
    wedge the worker deploy.
    """
    while True:
        try:
            loaded = await load_leaderboards(client, session_maker)
            matchtype_map.update(loaded)
            got_upstream = len(loaded) > len(DEFAULT_MATCHTYPE_TO_LEADERBOARD)
            sleep_for = refresh_seconds if got_upstream else retry_seconds
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("load_leaderboards_loader_failed", error=str(e))
            sleep_for = retry_seconds
        await asyncio.sleep(sleep_for)

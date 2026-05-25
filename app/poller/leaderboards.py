"""One-shot loader for the leaderboard metadata table.

``getAvailableLeaderboards`` rarely changes (new ladders land when Relic
publishes them); we call it once at startup, upsert each leaderboard
into the ``leaderboards`` table, and return the
``matchtype_id -> leaderboard_id`` map the recent-matches poller needs.
Daily refresh would be a v1.x ergonomic; the v1 trade-off is that adding
a leaderboard requires a restart.
"""

from __future__ import annotations

import httpx
import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.poller import status as poller_status
from app.poller.parsers import matchtype_to_leaderboard_map, parse_available_leaderboards
from app.poller.status import PollerSource
from app.poller.upserts import upsert_leaderboard

logger = structlog.get_logger(__name__)

_ENDPOINT = "/community/leaderboard/getAvailableLeaderboards"


async def load_leaderboards(
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker,
) -> dict[int, int]:
    """Fetch leaderboards, upsert each into the DB, return the matchtype mapping.

    The returned ``{matchtype_id: leaderboard_id}`` map is consumed by the
    recent-matches poller to fill ``Match.leaderboard_id`` without a
    second upstream call per match. Logs and returns an empty mapping on
    failure — the table stays unchanged, ``/v1/leaderboards`` returns the
    last successful snapshot, and the rest of the polling work continues
    unaffected.
    """
    try:
        response = await client.get(_ENDPOINT, params={"title": "age2"})
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.error("load_leaderboards_failed", error=str(e))
        return {}

    rows = parse_available_leaderboards(payload)
    async with session_maker() as session:
        for row in rows:
            await upsert_leaderboard(session, row)
        await session.commit()
    logger.info("load_leaderboards_ok", count=len(rows))
    poller_status.record_tick(PollerSource.LEADERBOARDS)
    return matchtype_to_leaderboard_map(payload)

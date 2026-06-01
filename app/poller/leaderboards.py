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

from app.poller.parsers import (
    DEFAULT_MATCHTYPE_TO_LEADERBOARD,
    matchtype_to_leaderboard_map,
    parse_available_leaderboards,
)
from app.poller.upserts import upsert_leaderboard

logger = structlog.get_logger(__name__)

_ENDPOINT = "/community/leaderboard/getAvailableLeaderboards"


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

"""One-shot loader for the static leaderboard metadata.

``getAvailableLeaderboards`` returns a payload that rarely changes (new
ladders or tournament-mode leaderboards land when Relic publishes them).
We call it once at startup, populate the in-memory cache, and let the
``/v1/leaderboards`` endpoint read from there. Daily refresh would be a
v1.x ergonomic; the v1 trade-off is that adding a leaderboard requires
a restart.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import structlog

from app import leaderboards_cache
from app.poller.parsers import matchtype_to_leaderboard_map, parse_available_leaderboards

logger = structlog.get_logger(__name__)

_ENDPOINT = "/community/leaderboard/getAvailableLeaderboards"


async def load_leaderboards(client: httpx.AsyncClient) -> dict[int, int]:
    """Fetch and cache leaderboard metadata; return the matchtype mapping.

    The returned ``{matchtype_id: leaderboard_id}`` map is consumed by the
    recent-matches poller to fill ``Match.leaderboard_id`` without a
    second upstream call per match. Logs and returns an empty mapping on
    failure — the cache stays empty, ``/v1/leaderboards`` returns ``[]``,
    and the rest of the polling work continues unaffected.
    """
    try:
        response = await client.get(_ENDPOINT, params={"title": "age2"})
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.error("load_leaderboards_failed", error=str(e))
        return {}

    items = parse_available_leaderboards(payload)
    leaderboards_cache.set_cache(items, refreshed_at=datetime.now(tz=UTC))
    logger.info("load_leaderboards_ok", count=len(items))
    return matchtype_to_leaderboard_map(payload)

"""Shared ``httpx.AsyncClient`` factory for the polling worker.

All four poller tasks (leaderboards loader + three runtime tasks) reuse
one client so connection pools and HTTP/2 sessions are shared. The client
is built once in the FastAPI lifespan startup and closed on shutdown.
"""

from __future__ import annotations

import httpx

from app.config import settings

# 10s per-call timeout: comfortably above the observed upstream p95 (307ms
# in the spike) but tight enough that a stalled upstream doesn't block the
# task loop past one cycle.
_DEFAULT_TIMEOUT_SECONDS = 10.0


def build_upstream_client() -> httpx.AsyncClient:
    """Construct the shared client targeting Relic's community surface.

    ``base_url`` comes from settings so tests can point at a respx mock or
    a local stub. All upstream calls in the poller use relative paths
    (e.g. ``/community/leaderboard/GetPersonalStat``) so swapping the base
    URL switches the whole worker.
    """
    return httpx.AsyncClient(
        base_url=settings.upstream_base_url,
        timeout=_DEFAULT_TIMEOUT_SECONDS,
        headers={"Accept": "application/json"},
    )

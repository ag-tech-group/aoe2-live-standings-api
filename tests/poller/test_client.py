"""Tests for the shared upstream HTTP client factory + its 429 log hook."""

import httpx
from structlog.testing import capture_logs

from app.poller.client import _log_rate_limits, build_upstream_client


async def test_logs_rate_limited_responses():
    # A 429 emits a distinct `upstream_rate_limited` event with Retry-After;
    # a 200 emits nothing.
    request = httpx.Request("GET", "https://aoe-api.example/community/x")
    with capture_logs() as logs:
        await _log_rate_limits(httpx.Response(429, request=request, headers={"retry-after": "5"}))
        await _log_rate_limits(httpx.Response(200, request=request))

    rate_limited = [e for e in logs if e["event"] == "upstream_rate_limited"]
    assert len(rate_limited) == 1
    assert rate_limited[0]["retry_after"] == "5"


async def test_client_wires_the_rate_limit_hook():
    client = build_upstream_client()
    try:
        assert _log_rate_limits in client.event_hooks["response"]
    finally:
        await client.aclose()

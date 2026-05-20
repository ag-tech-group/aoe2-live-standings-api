"""Tests for the GET /v1/stream SSE endpoint.

The endpoint is exercised at the generator level (``_event_stream``)
rather than over HTTP. httpx's ``ASGITransport`` buffers the whole
response body before returning, so a ``client.stream()`` against an
infinite SSE stream never returns — driving the async generator directly
is the way to test streaming logic in-process.
"""

import asyncio
from datetime import UTC, datetime

import pytest

from app.events import EventType, Nudge, hub
from app.main import app
from app.routers.stream import _event_stream, _format_nudge


class _FakeRequest:
    """Minimal stand-in for Starlette's Request — just the bit _event_stream uses."""

    def __init__(self) -> None:
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


class TestFormatNudge:
    def test_renders_an_sse_event_frame(self):
        nudge = Nudge(
            event=EventType.STANDINGS,
            polled_at=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
        )
        frame = _format_nudge(nudge)

        # SSE frame: an `event:` line, a `data:` line, terminated by a blank line.
        assert frame.startswith("event: standings\n")
        assert "data: " in frame
        assert frame.endswith("\n\n")
        assert "2026-05-20T12:00:00+00:00" in frame


class TestStreamRouteRegistered:
    def test_stream_route_is_mounted_under_v1(self):
        assert any(getattr(r, "path", None) == "/v1/stream" for r in app.routes)


class TestEventStream:
    async def test_yields_a_published_nudge(self):
        gen = _event_stream(_FakeRequest())
        # The generator subscribes on its first iteration — drive that as a
        # task, wait for the subscription, then publish.
        frame_task = asyncio.create_task(gen.__anext__())
        for _ in range(200):
            if hub.subscriber_count > 0:
                break
            await asyncio.sleep(0.01)
        hub.publish(EventType.STANDINGS)

        frame = await asyncio.wait_for(frame_task, timeout=5)
        assert "event: standings" in frame
        await gen.aclose()

    async def test_emits_heartbeat_when_idle(self, monkeypatch):
        # Shrink the heartbeat interval so the idle path fires fast.
        monkeypatch.setattr("app.routers.stream._HEARTBEAT_INTERVAL_SECONDS", 0.05)
        gen = _event_stream(_FakeRequest())

        frame = await asyncio.wait_for(gen.__anext__(), timeout=5)
        assert frame == ": heartbeat\n\n"
        await gen.aclose()

    async def test_stops_and_unsubscribes_when_client_disconnects(self):
        request = _FakeRequest()
        request.disconnected = True
        gen = _event_stream(request)

        # First iteration subscribes, sees the disconnect, breaks, and the
        # finally-block unsubscribes — so the generator is immediately done.
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen.__anext__(), timeout=5)
        assert hub.subscriber_count == 0

    async def test_unsubscribes_when_cancelled_mid_stream(self):
        """Cancellation is how Starlette tears the generator down on disconnect."""
        gen = _event_stream(_FakeRequest())
        frame_task = asyncio.create_task(gen.__anext__())
        for _ in range(200):
            if hub.subscriber_count > 0:
                break
            await asyncio.sleep(0.01)
        assert hub.subscriber_count == 1

        # Cancelling the in-flight __anext__ throws CancelledError into the
        # generator; it propagates through the finally-block (unsubscribe).
        frame_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await frame_task
        assert hub.subscriber_count == 0

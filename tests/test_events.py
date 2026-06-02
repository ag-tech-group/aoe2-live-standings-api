"""Unit tests for the SSE event hub."""

import asyncio

import pytest
from structlog.testing import capture_logs

from app.events import EventHub, EventType, sample_subscriber_count


class TestEventHub:
    async def test_subscribe_returns_distinct_queues(self):
        hub = EventHub()
        q1 = hub.subscribe()
        q2 = hub.subscribe()
        assert q1 is not q2
        assert hub.subscriber_count == 2

    async def test_unsubscribe_drops_the_subscriber(self):
        hub = EventHub()
        queue = hub.subscribe()
        hub.unsubscribe(queue)
        assert hub.subscriber_count == 0

    async def test_publish_fans_out_to_every_subscriber(self):
        hub = EventHub()
        q1 = hub.subscribe()
        q2 = hub.subscribe()

        hub.publish(EventType.STANDINGS)

        assert q1.get_nowait().event == EventType.STANDINGS
        assert q2.get_nowait().event == EventType.STANDINGS

    async def test_publish_with_no_subscribers_is_a_noop(self):
        hub = EventHub()
        hub.publish(EventType.LIVE)  # must not raise

    async def test_publish_drops_nudges_when_a_subscriber_queue_is_full(self):
        """A slow consumer can't back the queue up unboundedly or crash publish."""
        hub = EventHub()
        queue = hub.subscribe()

        # Publish far more than the queue can hold — none of these raise.
        for _ in range(100):
            hub.publish(EventType.MATCHES)

        assert queue.full()
        assert queue.qsize() <= 16  # _SUBSCRIBER_QUEUE_MAXSIZE

    async def test_nudge_carries_event_type_and_timestamp(self):
        hub = EventHub()
        queue = hub.subscribe()

        hub.publish(EventType.LIVE)

        nudge = queue.get_nowait()
        assert nudge.event == EventType.LIVE
        assert nudge.polled_at is not None


class TestSampleSubscriberCount:
    async def test_logs_the_live_count_each_tick(self, monkeypatch):
        """The sampler emits the hub's current subscriber count as a log event."""
        hub = EventHub()
        hub.subscribe()
        hub.subscribe()
        hub.subscribe()

        # Let one tick run, then break the loop by cancelling the next sleep.
        ticks = 0

        async def fake_sleep(_seconds):
            nonlocal ticks
            ticks += 1
            if ticks >= 2:
                raise asyncio.CancelledError

        monkeypatch.setattr("app.events.asyncio.sleep", fake_sleep)

        with capture_logs() as logs:
            with pytest.raises(asyncio.CancelledError):
                await sample_subscriber_count(hub)

        samples = [log for log in logs if log["event"] == "sse_subscriber_count"]
        assert len(samples) == 1
        assert samples[0]["count"] == 3

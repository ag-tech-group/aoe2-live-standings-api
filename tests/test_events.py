"""Unit tests for the SSE event hub."""

from app.events import EventHub, EventType


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

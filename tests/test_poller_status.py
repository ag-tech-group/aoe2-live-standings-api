"""Tests for `app.poller.status` and the `compute_last_polled_at`
fallback chain that consumes it (#9)."""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.poller import status as poller_status
from app.poller.status import PollerSource
from app.schemas.common import compute_last_polled_at
from tests.conftest import make_tournament


class TestPollerStatusModule:
    """Module-level state — record / read / reset."""

    def test_last_tick_is_none_before_any_record(self):
        # The autouse fixture `reset_poller_status` runs before every
        # test, so each test starts with a clean slate.
        assert poller_status.last_tick(PollerSource.PLAYER_STATS) is None

    def test_record_tick_with_explicit_when(self):
        ts = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
        poller_status.record_tick(PollerSource.PLAYER_STATS, when=ts)
        assert poller_status.last_tick(PollerSource.PLAYER_STATS) == ts

    def test_record_tick_defaults_to_now_utc(self):
        before = datetime.now(UTC)
        poller_status.record_tick(PollerSource.LIVE_MATCHES)
        after = datetime.now(UTC)

        tick = poller_status.last_tick(PollerSource.LIVE_MATCHES)
        assert tick is not None
        assert before <= tick <= after
        # All ticks should be tz-aware UTC.
        assert tick.tzinfo is UTC

    def test_each_source_has_its_own_slot(self):
        # A tick on one source must not leak into another.
        player_ts = datetime(2026, 5, 24, 9, 0, tzinfo=UTC)
        live_ts = datetime(2026, 5, 24, 10, 0, tzinfo=UTC)
        poller_status.record_tick(PollerSource.PLAYER_STATS, when=player_ts)
        poller_status.record_tick(PollerSource.LIVE_MATCHES, when=live_ts)
        assert poller_status.last_tick(PollerSource.PLAYER_STATS) == player_ts
        assert poller_status.last_tick(PollerSource.LIVE_MATCHES) == live_ts
        # Untouched sources stay None.
        assert poller_status.last_tick(PollerSource.RECENT_MATCHES) is None

    def test_record_tick_is_last_write_wins(self):
        first = datetime(2026, 5, 24, 9, 0, tzinfo=UTC)
        later = datetime(2026, 5, 24, 10, 0, tzinfo=UTC)
        poller_status.record_tick(PollerSource.PLAYER_STATS, when=first)
        poller_status.record_tick(PollerSource.PLAYER_STATS, when=later)
        assert poller_status.last_tick(PollerSource.PLAYER_STATS) == later


class TestComputeLastPolledAt:
    """`compute_last_polled_at` — fallback chain."""

    def test_no_source_no_rows_returns_none(self):
        assert compute_last_polled_at([]) is None

    def test_no_source_with_row_timestamps_returns_max(self):
        rows = [
            datetime(2026, 5, 24, 9, 0, tzinfo=UTC),
            None,
            datetime(2026, 5, 24, 10, 0, tzinfo=UTC),
        ]
        assert compute_last_polled_at(rows) == datetime(2026, 5, 24, 10, 0, tzinfo=UTC)

    def test_source_with_tick_takes_priority_over_row_max(self):
        # Even when rows have a more-recent updated_at, the tick wins
        # because it answers "when did we last check?" — the
        # canonical freshness question.
        poller_status.record_tick(
            PollerSource.PLAYER_STATS, when=datetime(2026, 5, 24, 8, 0, tzinfo=UTC)
        )
        rows = [datetime(2026, 5, 24, 12, 0, tzinfo=UTC)]
        assert compute_last_polled_at(rows, source=PollerSource.PLAYER_STATS) == datetime(
            2026, 5, 24, 8, 0, tzinfo=UTC
        )

    def test_source_without_tick_falls_back_to_row_max(self):
        # Process just started — the poller hasn't ticked yet, but the
        # DB has rows from a prior run. Row max is the right answer.
        rows = [datetime(2026, 5, 24, 9, 0, tzinfo=UTC)]
        assert compute_last_polled_at(rows, source=PollerSource.PLAYER_STATS) == datetime(
            2026, 5, 24, 9, 0, tzinfo=UTC
        )

    def test_source_without_tick_no_rows_returns_none(self):
        # The "haven't checked yet and no data" case — null is right.
        assert compute_last_polled_at([], source=PollerSource.PLAYER_STATS) is None


class TestEndpointFreshnessFromPollerTick:
    """End-to-end: a list endpoint returns the poller tick time as
    `last_polled_at` even when its result rows are empty — the headline
    case from #9 for the live-match feed."""

    async def test_empty_live_feed_uses_recorded_tick(
        self, client: AsyncClient, session: AsyncSession
    ):
        # No live matches in the DB. Before #9 this returned
        # last_polled_at=None; now it returns the poller's tick time.
        session.add(make_tournament("cup"))
        await session.commit()

        tick = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
        poller_status.record_tick(PollerSource.LIVE_MATCHES, when=tick)

        body = (await client.get("/v1/tournaments/cup/live")).json()
        assert body["items"] == []
        assert body["last_polled_at"] is not None
        # The tick time round-trips through JSON ISO serialization.
        assert body["last_polled_at"].startswith("2026-05-24T12:00")

    async def test_empty_standings_uses_player_stats_tick(
        self, client: AsyncClient, session: AsyncSession
    ):
        session.add(make_tournament("cup"))
        await session.commit()

        tick = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
        poller_status.record_tick(PollerSource.PLAYER_STATS, when=tick)

        body = (await client.get("/v1/tournaments/cup/standings")).json()
        assert body["items"] == []
        assert body["last_polled_at"].startswith("2026-05-24T12:00")

    async def test_empty_without_tick_still_null(self, client: AsyncClient, session: AsyncSession):
        # The "haven't ticked yet" case — preserve the pre-#9 null.
        session.add(make_tournament("cup"))
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/live")).json()
        assert body["items"] == []
        assert body["last_polled_at"] is None

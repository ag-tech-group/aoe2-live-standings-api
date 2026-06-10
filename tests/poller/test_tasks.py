"""Integration tests for the polling tasks: respx-mocked upstream + test DB.

These exercise the full fetch -> parse -> upsert path inside each
``tick_*`` function, without spinning up the long-running runner loops.
The runners themselves are thin wrappers around the ticks plus
``asyncio.sleep`` — tested manually if we ever doubt the loop scaffolding.
"""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Civilization,
    Leaderboard,
    LiveMatchPlayer,
    Match,
    MatchPlayer,
    MatchState,
    Player,
    PlayerRating,
)
from app.poller.leaderboards import load_leaderboards, run_leaderboards_loader
from app.poller.live_matches import tick_live_matches
from app.poller.parsers import DEFAULT_MATCHTYPE_TO_LEADERBOARD
from app.poller.player_stats import tick_player_stats
from app.poller.recent_matches import tick_recent_matches
from tests.conftest import async_session_maker as session_maker_for_tasks
from tests.conftest import make_match

_TEST_BASE_URL = "https://upstream.test"


@pytest.fixture
async def upstream_client():
    """An httpx client wired to the same base URL respx mocks register against."""
    async with httpx.AsyncClient(base_url=_TEST_BASE_URL) as client:
        yield client


class TestTickPlayerStats:
    async def test_writes_player_and_rating_rows(
        self, upstream_client: httpx.AsyncClient, session: AsyncSession
    ):
        payload = {
            "statGroups": [
                {
                    "id": 100,
                    "members": [
                        {
                            "profile_id": 199325,
                            "alias": "Hera",
                            "name": "/steam/76561198449406083",
                            "country": "ca",
                            "level": 4,
                            "xp": 12964,
                            "leaderboardregion_id": 3,
                            "clanlist_name": "",
                        }
                    ],
                }
            ],
            "leaderboardStats": [
                {
                    "statgroup_id": 100,
                    "leaderboard_id": 3,
                    "rating": 2788,
                    "highestrating": 3045,
                    "wins": 100,
                    "losses": 10,
                    "streak": 5,
                    "drops": 0,
                    "rank": 1,
                    "ranktotal": 10000,
                    "regionrank": 1,
                    "regionranktotal": 1000,
                    "lastmatchdate": 1779084162,
                }
            ],
        }

        with respx.mock(base_url=_TEST_BASE_URL) as mock:
            mock.get("/community/leaderboard/GetPersonalStat").respond(json=payload)
            await tick_player_stats(upstream_client, [199325], session_maker_for_tasks)

        player = (await session.execute(select(Player))).scalar_one()
        assert player.alias == "Hera"
        assert player.steam_id == "76561198449406083"

        rating = (await session.execute(select(PlayerRating))).scalar_one()
        assert rating.current_rating == 2788
        assert rating.rank == 1

    async def test_skips_upstream_when_no_tracked_profiles(
        self, upstream_client: httpx.AsyncClient
    ):
        # No respx route registered — if the tick tried to call upstream, it'd
        # raise a ConnectError. The empty-list early return is what we're verifying.
        with respx.mock(base_url=_TEST_BASE_URL):
            await tick_player_stats(upstream_client, [], session_maker_for_tasks)


class TestTickRecentMatches:
    async def test_writes_matches_and_match_players(
        self, upstream_client: httpx.AsyncClient, session: AsyncSession
    ):
        payload = {
            "matchHistoryStats": [
                {
                    "id": 1,
                    "mapname": "Arabia.rms",
                    "matchtype_id": 6,
                    "startgametime": 1779000000,
                    "completiontime": 1779001000,
                    "matchhistoryreportresults": [
                        {
                            "profile_id": 199325,
                            "civilization_id": 16,
                            "teamid": 1,
                            "resulttype": 1,
                            "xpgained": 1,
                        }
                    ],
                    "matchhistorymember": [
                        {"profile_id": 199325, "oldrating": 1500, "newrating": 1510}
                    ],
                }
            ]
        }

        with respx.mock(base_url=_TEST_BASE_URL) as mock:
            mock.get("/community/leaderboard/getRecentMatchHistory").respond(json=payload)
            await tick_recent_matches(
                upstream_client,
                [199325],
                session_maker_for_tasks,
                matchtype_to_leaderboard={6: 3},
            )

        match = (await session.execute(select(Match))).scalar_one()
        assert match.match_id == 1
        assert match.leaderboard_id == 3

        mp = (await session.execute(select(MatchPlayer))).scalar_one()
        assert mp.profile_id == 199325
        assert mp.new_rating == 1510

    async def test_one_failing_profile_does_not_kill_the_batch(
        self, upstream_client: httpx.AsyncClient, session: AsyncSession
    ):
        ok_payload = {
            "matchHistoryStats": [
                {
                    "id": 1,
                    "mapname": "x.rms",
                    "matchtype_id": 6,
                    "startgametime": 1,
                    "completiontime": 2,
                    "matchhistoryreportresults": [],
                    "matchhistorymember": [],
                }
            ]
        }

        with respx.mock(base_url=_TEST_BASE_URL) as mock:
            route = mock.get("/community/leaderboard/getRecentMatchHistory")
            route.side_effect = [
                httpx.Response(500),
                httpx.Response(200, json=ok_payload),
            ]
            await tick_recent_matches(
                upstream_client,
                [1, 2],
                session_maker_for_tasks,
                matchtype_to_leaderboard={6: 3},
            )

        matches = (await session.execute(select(Match))).scalars().all()
        assert len(matches) == 1

    async def test_upstream_outage_logs_one_aggregated_error(
        self, upstream_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        # An upstream outage fails every profile in the fan-out. The cycle
        # must emit ONE aggregated error (not one per profile), carrying a
        # status breakdown — so a worldsedgelink 502 storm collapses to a
        # single log line / Sentry event instead of N.
        errors: list[tuple[str, dict]] = []

        class _RecordingLogger:
            def error(self, event: str, **kw: object) -> None:
                errors.append((event, kw))

            def info(self, *args: object, **kw: object) -> None:
                pass

        monkeypatch.setattr("app.poller.recent_matches.logger", _RecordingLogger())

        with respx.mock(base_url=_TEST_BASE_URL) as mock:
            mock.get("/community/leaderboard/getRecentMatchHistory").respond(502)
            await tick_recent_matches(
                upstream_client,
                [1, 2, 3],
                session_maker_for_tasks,
                matchtype_to_leaderboard={6: 3},
            )

        assert len(errors) == 1
        event, kw = errors[0]
        assert event == "recent_matches_fetch_failed"
        assert kw["failed"] == 3
        assert kw["total"] == 3
        assert kw["statuses"] == {502: 3}
        assert sorted(kw["profile_ids"]) == [1, 2, 3]


class TestTickLiveMatches:
    async def test_writes_only_tracked_lobbies(
        self, upstream_client: httpx.AsyncClient, session: AsyncSession
    ):
        payload = {
            "matches": [
                {
                    "id": 1,
                    "mapname": "Arabia.rms",
                    "matchtype_id": 0,
                    "state": 0,
                    "matchmembers": [{"profile_id": 199325}, {"profile_id": 409748}],
                },
                {
                    "id": 2,
                    "mapname": "Arena.rms",
                    "matchtype_id": 0,
                    "state": 0,
                    "matchmembers": [{"profile_id": 99}, {"profile_id": 100}],
                },
            ]
        }

        with respx.mock(base_url=_TEST_BASE_URL) as mock:
            mock.get("/community/advertisement/findAdvertisements").respond(json=payload)
            await tick_live_matches(upstream_client, [199325], session_maker_for_tasks)

        matches = (await session.execute(select(Match))).scalars().all()
        assert [m.match_id for m in matches] == [1]
        assert matches[0].state == MatchState.STAGING

    async def test_writes_live_match_players_for_tracked_profiles(
        self, upstream_client: httpx.AsyncClient, session: AsyncSession
    ):
        payload = {
            "matches": [
                {
                    "id": 1,
                    "mapname": "Arabia.rms",
                    "matchtype_id": 0,
                    "state": 0,
                    "matchmembers": [{"profile_id": 199325}, {"profile_id": 409748}],
                }
            ]
        }
        with respx.mock(base_url=_TEST_BASE_URL) as mock:
            mock.get("/community/advertisement/findAdvertisements").respond(json=payload)
            await tick_live_matches(upstream_client, [199325], session_maker_for_tasks)

        live = (await session.execute(select(LiveMatchPlayer))).scalars().all()
        # Only the tracked member is linked; the untracked opponent is not.
        assert [(r.match_id, r.profile_id) for r in live] == [(1, 199325)]

    async def test_does_not_overwrite_completed_match(
        self, upstream_client: httpx.AsyncClient, session: AsyncSession
    ):
        """End-to-end version of the unit test in test_upserts: stale live data is ignored."""
        session.add(
            make_match(
                1,
                state=MatchState.COMPLETED,
                completed_at=datetime(2026, 5, 18, 12, 30, tzinfo=UTC),
            )
        )
        await session.commit()

        payload = {
            "matches": [
                {
                    "id": 1,
                    "mapname": "x.rms",
                    "matchtype_id": 0,
                    "state": 1,
                    "matchmembers": [{"profile_id": 199325}],
                }
            ]
        }
        with respx.mock(base_url=_TEST_BASE_URL) as mock:
            mock.get("/community/advertisement/findAdvertisements").respond(json=payload)
            await tick_live_matches(upstream_client, [199325], session_maker_for_tasks)

        await session.refresh((await session.execute(select(Match))).scalar_one())
        loaded = (await session.execute(select(Match))).scalar_one()
        assert loaded.state == MatchState.COMPLETED


class TestLoadLeaderboards:
    async def test_populates_db_and_returns_matchtype_map(
        self, upstream_client: httpx.AsyncClient, session: AsyncSession
    ):
        payload = {
            "leaderboards": [
                {"id": 3, "name": "1v1 RM Ranked", "isranked": 1, "matchtypes": [6]},
                {"id": 4, "name": "Team RM Ranked", "isranked": 1, "matchtypes": [7, 8]},
            ],
            "races": [
                {"id": 7, "name": "Burgundians"},
                {"id": 0, "name": "Armenians"},
            ],
        }
        with respx.mock(base_url=_TEST_BASE_URL) as mock:
            mock.get("/community/leaderboard/getAvailableLeaderboards").respond(json=payload)
            mapping = await load_leaderboards(upstream_client, session_maker_for_tasks)

        rows = (await session.execute(select(Leaderboard))).scalars().all()
        assert {lb.leaderboard_id for lb in rows} == {3, 4}
        assert mapping == {6: 3, 7: 4, 8: 4}
        # Civilizations from the same payload's `races` are upserted too (#227).
        civs = (await session.execute(select(Civilization))).scalars().all()
        assert {(c.civilization_id, c.name) for c in civs} == {(7, "Burgundians"), (0, "Armenians")}

    async def test_upstream_error_raises(
        self, upstream_client: httpx.AsyncClient, session: AsyncSession
    ):
        # A fetch failure now propagates (the loader catches it, keeps the
        # lifespan-seeded floor, and retries soon) rather than masquerading as a
        # healthy load that returned the floor — which is what forced the
        # tight-retry loop in #182.
        with respx.mock(base_url=_TEST_BASE_URL) as mock:
            mock.get("/community/leaderboard/getAvailableLeaderboards").respond(500)
            with pytest.raises(httpx.HTTPError):
                await load_leaderboards(upstream_client, session_maker_for_tasks)

        # The table stays unchanged on failure.
        rows = (await session.execute(select(Leaderboard))).scalars().all()
        assert rows == []

    async def test_empty_matchtypes_falls_back_to_floor(
        self, upstream_client: httpx.AsyncClient, session: AsyncSession
    ):
        # Upstream regression observed 2026-06-01: leaderboards present but with
        # missing/empty matchtypes. Previously the derived map came back empty
        # and every match was written with null leaderboard_id, silently
        # emptying tournament standings. The floor now backstops that.
        payload = {
            "leaderboards": [
                {"id": 3, "name": "1v1 RM Ranked", "isranked": 1},
                {"id": 4, "name": "Team RM Ranked", "isranked": 1, "matchtypes": []},
            ]
        }
        with respx.mock(base_url=_TEST_BASE_URL) as mock:
            mock.get("/community/leaderboard/getAvailableLeaderboards").respond(json=payload)
            mapping = await load_leaderboards(upstream_client, session_maker_for_tasks)

        # Rows still upsert; the map falls back to the static floor so the core
        # ladder (matchtype 6 -> leaderboard 3) keeps tagging matches.
        rows = (await session.execute(select(Leaderboard))).scalars().all()
        assert {lb.leaderboard_id for lb in rows} == {3, 4}
        assert mapping == DEFAULT_MATCHTYPE_TO_LEADERBOARD


class TestRunLeaderboardsLoader:
    async def test_updates_shared_map_in_place(
        self, upstream_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        # lifespan seeds the map with the floor; the loader enriches that same
        # dict in place so the recent-matches poller (which holds the reference)
        # picks up upstream mappings without a restart.
        matchtype_map = dict(DEFAULT_MATCHTYPE_TO_LEADERBOARD)

        async def fake_load(client, session_maker):
            return {6: 3, 7: 3, 8: 4}

        async def stop_after_first(_seconds):
            raise asyncio.CancelledError

        monkeypatch.setattr("app.poller.leaderboards.load_leaderboards", fake_load)
        monkeypatch.setattr(asyncio, "sleep", stop_after_first)

        with pytest.raises(asyncio.CancelledError):
            await run_leaderboards_loader(upstream_client, session_maker_for_tasks, matchtype_map)

        assert matchtype_map == {6: 3, 7: 3, 8: 4}

    async def test_load_failure_keeps_floor_and_survives(
        self, upstream_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        # A DB error during the upsert (the connection-saturation case) must be
        # caught and retried, never killing the task or clobbering the seeded
        # floor — this is what lets the worker start during a DB incident (#177).
        matchtype_map = dict(DEFAULT_MATCHTYPE_TO_LEADERBOARD)

        async def fake_load_raises(client, session_maker):
            raise RuntimeError("remaining connection slots are reserved")

        async def stop_after_first(_seconds):
            raise asyncio.CancelledError

        monkeypatch.setattr("app.poller.leaderboards.load_leaderboards", fake_load_raises)
        monkeypatch.setattr(asyncio, "sleep", stop_after_first)

        with pytest.raises(asyncio.CancelledError):
            await run_leaderboards_loader(upstream_client, session_maker_for_tasks, matchtype_map)

        assert matchtype_map == DEFAULT_MATCHTYPE_TO_LEADERBOARD

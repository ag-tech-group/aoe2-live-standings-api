"""Integration tests for the polling tasks: respx-mocked upstream + test DB.

These exercise the full fetch -> parse -> upsert path inside each
``tick_*`` function, without spinning up the long-running runner loops.
The runners themselves are thin wrappers around the ticks plus
``asyncio.sleep`` — tested manually if we ever doubt the loop scaffolding.
"""

from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import leaderboards_cache
from app.events import EventType, hub
from app.models import Match, MatchPlayer, MatchState, Player, PlayerRating
from app.poller.leaderboards import load_leaderboards
from app.poller.live_matches import tick_live_matches
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

        nudges = hub.subscribe()
        with respx.mock(base_url=_TEST_BASE_URL) as mock:
            mock.get("/community/leaderboard/GetPersonalStat").respond(json=payload)
            await tick_player_stats(upstream_client, [199325], session_maker_for_tasks)

        player = (await session.execute(select(Player))).scalar_one()
        assert player.alias == "Hera"
        assert player.steam_id == "76561198449406083"

        rating = (await session.execute(select(PlayerRating))).scalar_one()
        assert rating.current_rating == 2788
        assert rating.rank == 1

        # A successful tick nudges SSE subscribers.
        assert nudges.get_nowait().event == EventType.STANDINGS

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

        nudges = hub.subscribe()
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

        assert nudges.get_nowait().event == EventType.MATCHES

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


class TestTickLiveMatches:
    async def test_writes_only_tracked_lobbies(
        self, upstream_client: httpx.AsyncClient, session: AsyncSession
    ):
        payload = {
            "advertisements": [
                {
                    "match_id": 1,
                    "mapname": "Arabia.rms",
                    "matchtype_id": 0,
                    "creation_time": 1779000000,
                    "state": 0,
                    "matchmembers": [{"profile_id": 199325}, {"profile_id": 409748}],
                },
                {
                    "match_id": 2,
                    "mapname": "Arena.rms",
                    "matchtype_id": 0,
                    "creation_time": 1779000100,
                    "state": 0,
                    "matchmembers": [{"profile_id": 99}, {"profile_id": 100}],
                },
            ]
        }

        nudges = hub.subscribe()
        with respx.mock(base_url=_TEST_BASE_URL) as mock:
            mock.get("/community/advertisement/findAdvertisements").respond(json=payload)
            await tick_live_matches(upstream_client, [199325], session_maker_for_tasks)

        matches = (await session.execute(select(Match))).scalars().all()
        assert [m.match_id for m in matches] == [1]
        assert matches[0].state == MatchState.STAGING

        assert nudges.get_nowait().event == EventType.LIVE

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
            "advertisements": [
                {
                    "match_id": 1,
                    "mapname": "x.rms",
                    "matchtype_id": 0,
                    "creation_time": 1,
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
    async def test_populates_cache_and_returns_matchtype_map(
        self, upstream_client: httpx.AsyncClient
    ):
        payload = {
            "leaderboards": [
                {"id": 3, "name": "1v1 RM Ranked", "isranked": 1, "matchtypes": [6]},
                {"id": 4, "name": "Team RM Ranked", "isranked": 1, "matchtypes": [7, 8]},
            ]
        }
        with respx.mock(base_url=_TEST_BASE_URL) as mock:
            mock.get("/community/leaderboard/getAvailableLeaderboards").respond(json=payload)
            mapping = await load_leaderboards(upstream_client)

        cached = leaderboards_cache.get_cache()
        assert {lb.leaderboard_id for lb in cached} == {3, 4}
        assert mapping == {6: 3, 7: 4, 8: 4}

    async def test_upstream_error_logs_and_returns_empty(self, upstream_client: httpx.AsyncClient):
        with respx.mock(base_url=_TEST_BASE_URL) as mock:
            mock.get("/community/leaderboard/getAvailableLeaderboards").respond(500)
            mapping = await load_leaderboards(upstream_client)

        assert mapping == {}
        assert leaderboards_cache.get_cache() == ()

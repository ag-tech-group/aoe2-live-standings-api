"""Broadcast-live poller tests (#112): tick logic against the test DB.

The Twitch/YouTube clients are stubbed (their HTTP paths are covered in
test_broadcast.py); these exercise the URL->roster-row folding, the
platform-partitioned writes, and the change-detection that gates nudges.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LiveStream, Tournament, TournamentPlayer
from app.poller.live_streams import (
    _apply_live_set,
    tick_twitch_live,
    tick_youtube_live,
)
from tests.conftest import async_session_maker as session_maker_for_tasks


class _StubTwitch:
    """Returns the requested logins that are in a preset live set."""

    def __init__(self, live: set[str]) -> None:
        self._live = live

    async def get_live_logins(self, logins: list[str]) -> set[str]:
        return {login for login in logins if login in self._live}


class _StubYouTube:
    def __init__(self, live: set[tuple[str, str]]) -> None:
        self._live = live

    async def get_live_refs(self, refs: list[tuple[str, str]]) -> set[tuple[str, str]]:
        return {ref for ref in refs if ref in self._live}


async def _rows(session: AsyncSession) -> set[tuple[int, str]]:
    result = await session.execute(select(LiveStream))
    return {(r.tournament_player_id, r.platform) for r in result.scalars().all()}


@pytest.fixture
async def roster_row_ids(session: AsyncSession) -> dict[int, int]:
    """Return a mapping of {1: row1.id, 2: row2.id, 9: row9.id, ...}.

    Tests reference roster rows by stable small ints (1, 2, 9). This fixture
    persists a tournament with those rows and returns the surrogate ids so
    a test can drive ``stream_urls`` keyed by surrogate id.
    """
    tournament = Tournament(slug="test", name="Test", leaderboard_id=3)
    tournament.tracked_players = [
        TournamentPlayer(profile_id=1),
        TournamentPlayer(profile_id=2),
        TournamentPlayer(profile_id=9),
    ]
    session.add(tournament)
    await session.commit()
    return {p.profile_id: p.id for p in tournament.tracked_players}


class TestTickTwitchLive:
    async def test_writes_live_rows(self, session: AsyncSession, roster_row_ids: dict[int, int]):
        twitch = _StubTwitch(live={"grubby"})
        stream_urls = {
            roster_row_ids[1]: ["https://twitch.tv/Grubby"],
            roster_row_ids[2]: ["https://twitch.tv/day9tv"],
        }
        await tick_twitch_live(twitch, stream_urls, session_maker_for_tasks)
        assert await _rows(session) == {(roster_row_ids[1], "twitch")}

    async def test_clears_when_nobody_live(
        self, session: AsyncSession, roster_row_ids: dict[int, int]
    ):
        session.add(LiveStream(tournament_player_id=roster_row_ids[9], platform="twitch"))
        await session.commit()
        await tick_twitch_live(
            _StubTwitch(live=set()),
            {roster_row_ids[1]: ["https://twitch.tv/a"]},
            session_maker_for_tasks,
        )
        assert await _rows(session) == set()


class TestTickYouTubeLive:
    async def test_skips_players_with_a_twitch_link(
        self, session: AsyncSession, roster_row_ids: dict[int, int]
    ):
        # Row 1 has Twitch (handled by the Twitch poller) — never costs
        # YouTube quota even though it also lists a YouTube channel. Row 2
        # is YouTube-only, so it's the only one checked.
        youtube = _StubYouTube(live={("handle", "@spiff")})
        stream_urls = {
            roster_row_ids[1]: [
                "https://twitch.tv/grubby",
                "https://youtube.com/@grubby",
            ],
            roster_row_ids[2]: ["https://youtube.com/@spiff"],
        }
        await tick_youtube_live(youtube, stream_urls, session_maker_for_tasks)
        assert await _rows(session) == {(roster_row_ids[2], "youtube")}


class TestApplyLiveSet:
    async def test_reports_change_and_persists(
        self, session: AsyncSession, roster_row_ids: dict[int, int]
    ):
        live = {roster_row_ids[1], roster_row_ids[2]}
        assert await _apply_live_set(session_maker_for_tasks, "twitch", live) is True
        assert await _rows(session) == {
            (roster_row_ids[1], "twitch"),
            (roster_row_ids[2], "twitch"),
        }

    async def test_no_change_when_set_is_stable(
        self, session: AsyncSession, roster_row_ids: dict[int, int]
    ):
        live = {roster_row_ids[1], roster_row_ids[2]}
        await _apply_live_set(session_maker_for_tasks, "twitch", live)
        # Same set again -> no rewrite, no nudge.
        assert await _apply_live_set(session_maker_for_tasks, "twitch", live) is False

    async def test_platforms_do_not_clobber_each_other(
        self, session: AsyncSession, roster_row_ids: dict[int, int]
    ):
        await _apply_live_set(session_maker_for_tasks, "twitch", {roster_row_ids[1]})
        await _apply_live_set(session_maker_for_tasks, "youtube", {roster_row_ids[2]})
        # Rewriting twitch leaves the youtube row intact.
        await _apply_live_set(session_maker_for_tasks, "twitch", {roster_row_ids[9]})
        assert await _rows(session) == {
            (roster_row_ids[9], "twitch"),
            (roster_row_ids[2], "youtube"),
        }

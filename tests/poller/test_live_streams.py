"""Broadcast-live poller tests (#112): tick logic against the test DB.

The Twitch/YouTube clients are stubbed (their HTTP paths are covered in
test_broadcast.py); these exercise the URL->profile folding, the
platform-partitioned writes, and the change-detection that gates nudges.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LiveStream
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
    return {(r.profile_id, r.platform) for r in result.scalars().all()}


class TestTickTwitchLive:
    async def test_writes_live_profiles(self, session: AsyncSession):
        twitch = _StubTwitch(live={"grubby"})
        stream_urls = {
            1: ["https://twitch.tv/Grubby"],
            2: ["https://twitch.tv/day9tv"],
        }
        await tick_twitch_live(twitch, stream_urls, session_maker_for_tasks)
        assert await _rows(session) == {(1, "twitch")}

    async def test_clears_when_nobody_live(self, session: AsyncSession):
        session.add(LiveStream(profile_id=9, platform="twitch"))
        await session.commit()
        await tick_twitch_live(
            _StubTwitch(live=set()), {1: ["https://twitch.tv/a"]}, session_maker_for_tasks
        )
        assert await _rows(session) == set()


class TestTickYouTubeLive:
    async def test_skips_players_with_a_twitch_link(self, session: AsyncSession):
        # Profile 1 has Twitch (handled by the Twitch poller) — never costs
        # YouTube quota even though it also lists a YouTube channel. Profile
        # 2 is YouTube-only, so it's the only one checked.
        youtube = _StubYouTube(live={("handle", "@spiff")})
        stream_urls = {
            1: ["https://twitch.tv/grubby", "https://youtube.com/@grubby"],
            2: ["https://youtube.com/@spiff"],
        }
        await tick_youtube_live(youtube, stream_urls, session_maker_for_tasks)
        assert await _rows(session) == {(2, "youtube")}


class TestApplyLiveSet:
    async def test_reports_change_and_persists(self, session: AsyncSession):
        assert await _apply_live_set(session_maker_for_tasks, "twitch", {1, 2}) is True
        assert await _rows(session) == {(1, "twitch"), (2, "twitch")}

    async def test_no_change_when_set_is_stable(self, session: AsyncSession):
        await _apply_live_set(session_maker_for_tasks, "twitch", {1, 2})
        # Same set again -> no rewrite, no nudge.
        assert await _apply_live_set(session_maker_for_tasks, "twitch", {1, 2}) is False

    async def test_platforms_do_not_clobber_each_other(self, session: AsyncSession):
        await _apply_live_set(session_maker_for_tasks, "twitch", {1})
        await _apply_live_set(session_maker_for_tasks, "youtube", {2})
        # Rewriting twitch leaves the youtube row intact.
        await _apply_live_set(session_maker_for_tasks, "twitch", {3})
        assert await _rows(session) == {(3, "twitch"), (2, "youtube")}

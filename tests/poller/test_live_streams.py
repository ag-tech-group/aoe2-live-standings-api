"""Broadcast-live poller tests (#112, #149): tick logic against the test DB.

The Twitch/YouTube clients are stubbed (their HTTP paths are covered in
test_broadcast.py); these exercise the URL->roster-row + URL->host-tournament
folding, the platform-partitioned writes, and the change-detection that
gates nudges.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import HostLiveStream, LiveStream, Tournament, TournamentPlayer
from app.poller.live_streams import (
    _apply_live_sets,
    tick_twitch_live,
    tick_youtube_live,
)
from tests.conftest import async_session_maker as session_maker_for_tasks


class _StubTwitch:
    """Returns the requested logins that are in a preset live set."""

    def __init__(self, live: set[str]) -> None:
        self._live = live
        self.calls: list[list[str]] = []

    async def get_live_logins(self, logins: list[str]) -> set[str]:
        self.calls.append(list(logins))
        return {login for login in logins if login in self._live}


class _StubYouTube:
    def __init__(self, live: set[tuple[str, str]]) -> None:
        self._live = live
        self.calls: list[list[tuple[str, str]]] = []

    async def get_live_refs(self, refs: list[tuple[str, str]]) -> set[tuple[str, str]]:
        self.calls.append(list(refs))
        return {ref for ref in refs if ref in self._live}


async def _live_stream_rows(session: AsyncSession) -> set[tuple[int, str]]:
    result = await session.execute(select(LiveStream))
    return {(r.tournament_player_id, r.platform) for r in result.scalars().all()}


async def _host_live_rows(session: AsyncSession) -> set[tuple[int, str]]:
    result = await session.execute(select(HostLiveStream))
    return {(r.tournament_id, r.platform) for r in result.scalars().all()}


@pytest.fixture
async def roster_row_ids(session: AsyncSession) -> dict[int, int]:
    """Return a mapping of {1: row1.id, 2: row2.id, 9: row9.id, ...}.

    Tests reference roster rows by stable small ints (1, 2, 9). This fixture
    persists a tournament with those rows and returns the surrogate ids so
    a test can drive ``stream_urls`` keyed by surrogate id.
    """
    tournament = Tournament(slug="test", name="Test", leaderboard_id=3)
    tournament.tracked_players = [
        TournamentPlayer(profile_id=1, name="p1"),
        TournamentPlayer(profile_id=2, name="p2"),
        TournamentPlayer(profile_id=9, name="p9"),
    ]
    session.add(tournament)
    await session.commit()
    return {p.profile_id: p.id for p in tournament.tracked_players}


@pytest.fixture
async def tournament_id(session: AsyncSession) -> int:
    """A bare tournament for host-channel tests that don't care about roster."""
    tournament = Tournament(slug="host-test", name="Host Test", leaderboard_id=3)
    session.add(tournament)
    await session.commit()
    return tournament.id


class TestTickTwitchLive:
    async def test_writes_live_rows(self, session: AsyncSession, roster_row_ids: dict[int, int]):
        twitch = _StubTwitch(live={"grubby"})
        stream_urls = {
            roster_row_ids[1]: ["https://twitch.tv/Grubby"],
            roster_row_ids[2]: ["https://twitch.tv/day9tv"],
        }
        await tick_twitch_live(twitch, stream_urls, {}, session_maker_for_tasks)
        assert await _live_stream_rows(session) == {(roster_row_ids[1], "twitch")}
        # No host channels supplied → host_live_streams stays empty.
        assert await _host_live_rows(session) == set()

    async def test_clears_when_nobody_live(
        self, session: AsyncSession, roster_row_ids: dict[int, int]
    ):
        session.add(LiveStream(tournament_player_id=roster_row_ids[9], platform="twitch"))
        await session.commit()
        await tick_twitch_live(
            _StubTwitch(live=set()),
            {roster_row_ids[1]: ["https://twitch.tv/a"]},
            {},
            session_maker_for_tasks,
        )
        assert await _live_stream_rows(session) == set()

    async def test_writes_live_host_tournaments(self, session: AsyncSession, tournament_id: int):
        """#149: host channels fold into host_live_streams via the same Helix call."""
        twitch = _StubTwitch(live={"hostchannel"})
        await tick_twitch_live(
            twitch,
            {},
            {tournament_id: ["https://twitch.tv/HostChannel"]},
            session_maker_for_tasks,
        )
        assert await _host_live_rows(session) == {(tournament_id, "twitch")}
        # And the live_streams table is untouched.
        assert await _live_stream_rows(session) == set()

    async def test_single_helix_call_covers_roster_and_host(
        self, session: AsyncSession, roster_row_ids: dict[int, int], tournament_id: int
    ):
        """One batched Twitch call covers both audiences per tick — no double-charge."""
        twitch = _StubTwitch(live={"grubby", "hostchannel"})
        stream_urls = {roster_row_ids[1]: ["https://twitch.tv/Grubby"]}
        host_urls = {tournament_id: ["https://twitch.tv/HostChannel"]}
        await tick_twitch_live(twitch, stream_urls, host_urls, session_maker_for_tasks)
        assert len(twitch.calls) == 1
        # Both logins land in the same Helix call (set membership, no ordering).
        assert set(twitch.calls[0]) == {"grubby", "hostchannel"}


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
        await tick_youtube_live(youtube, stream_urls, {}, session_maker_for_tasks)
        assert await _live_stream_rows(session) == {(roster_row_ids[2], "youtube")}

    async def test_writes_live_host_tournaments(self, session: AsyncSession, tournament_id: int):
        """#149: host channels participate in the YouTube poll too."""
        youtube = _StubYouTube(live={("handle", "@hostchan")})
        await tick_youtube_live(
            youtube,
            {},
            {tournament_id: ["https://youtube.com/@hostchan"]},
            session_maker_for_tasks,
        )
        assert await _host_live_rows(session) == {(tournament_id, "youtube")}


class TestApplyLiveSets:
    async def test_reports_change_when_roster_changes(
        self, session: AsyncSession, roster_row_ids: dict[int, int]
    ):
        live = {roster_row_ids[1], roster_row_ids[2]}
        assert await _apply_live_sets(session_maker_for_tasks, "twitch", live, set()) is True
        assert await _live_stream_rows(session) == {
            (roster_row_ids[1], "twitch"),
            (roster_row_ids[2], "twitch"),
        }

    async def test_reports_change_when_only_host_changes(
        self, session: AsyncSession, tournament_id: int
    ):
        """Either snapshot flipping is enough to trip the nudge — they share a SSE event."""
        assert (
            await _apply_live_sets(session_maker_for_tasks, "twitch", set(), {tournament_id})
            is True
        )
        assert await _host_live_rows(session) == {(tournament_id, "twitch")}

    async def test_no_change_when_both_sets_stable(
        self, session: AsyncSession, roster_row_ids: dict[int, int], tournament_id: int
    ):
        live = {roster_row_ids[1]}
        hosts = {tournament_id}
        await _apply_live_sets(session_maker_for_tasks, "twitch", live, hosts)
        # Same sets again -> no rewrite, no nudge.
        assert await _apply_live_sets(session_maker_for_tasks, "twitch", live, hosts) is False

    async def test_platforms_do_not_clobber_each_other(
        self, session: AsyncSession, roster_row_ids: dict[int, int], tournament_id: int
    ):
        await _apply_live_sets(
            session_maker_for_tasks, "twitch", {roster_row_ids[1]}, {tournament_id}
        )
        await _apply_live_sets(session_maker_for_tasks, "youtube", {roster_row_ids[2]}, set())
        # Rewriting twitch leaves the youtube rows intact for both snapshots.
        await _apply_live_sets(session_maker_for_tasks, "twitch", {roster_row_ids[9]}, set())
        assert await _live_stream_rows(session) == {
            (roster_row_ids[9], "twitch"),
            (roster_row_ids[2], "youtube"),
        }
        # Twitch host was cleared by the third call (empty set for that platform);
        # the youtube partition is independent.
        assert await _host_live_rows(session) == set()

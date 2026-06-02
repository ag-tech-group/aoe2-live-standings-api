"""Tests for the poller's tournament-roster resolution."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Tournament, TournamentPlayer
from app.poller.roster import (
    get_host_stream_urls_by_tournament,
    get_stream_urls_by_roster_row,
    get_tracked_profile_ids,
)


class TestGetTrackedProfileIds:
    async def test_returns_union_across_tournaments(self, session: AsyncSession):
        first = Tournament(slug="a", name="A", leaderboard_id=3)
        first.tracked_players = [
            TournamentPlayer(profile_id=1, name="p1"),
            TournamentPlayer(profile_id=2, name="p2"),
        ]
        second = Tournament(slug="b", name="B", leaderboard_id=4)
        second.tracked_players = [
            TournamentPlayer(profile_id=2, name="p2"),
            TournamentPlayer(profile_id=9, name="p9"),
        ]
        session.add_all([first, second])
        await session.commit()

        ids = await get_tracked_profile_ids(session)
        assert sorted(ids) == [1, 2, 9]

    async def test_empty_when_no_tournaments(self, session: AsyncSession):
        assert await get_tracked_profile_ids(session) == []


class TestGetStreamUrlsByRosterRow:
    async def test_includes_polled_and_placeholder_rows(self, session: AsyncSession):
        """#147: placeholder rows participate in broadcast-live detection."""
        tournament = Tournament(slug="cup", name="Cup", leaderboard_id=3)
        polled = TournamentPlayer(
            profile_id=1, name="p1", presentation={"streamUrls": ["https://twitch.tv/p1"]}
        )
        placeholder = TournamentPlayer(
            name="iyouxin", presentation={"streamUrls": ["https://twitch.tv/iyouxin"]}
        )
        tournament.tracked_players = [polled, placeholder]
        session.add(tournament)
        await session.commit()

        by_row = await get_stream_urls_by_roster_row(session)
        assert by_row == {
            polled.id: ["https://twitch.tv/p1"],
            placeholder.id: ["https://twitch.tv/iyouxin"],
        }


class TestGetHostStreamUrlsByTournament:
    async def test_returns_each_tournaments_host_urls(self, session: AsyncSession):
        """#149: per-tournament host URLs feed broadcast-live detection."""
        with_host = Tournament(
            slug="hosted",
            name="Hosted",
            leaderboard_id=3,
            host_stream_urls=["https://twitch.tv/host", "https://youtube.com/@host"],
        )
        without_host = Tournament(slug="quiet", name="Quiet", leaderboard_id=3)
        session.add_all([with_host, without_host])
        await session.commit()

        by_tournament = await get_host_stream_urls_by_tournament(session)
        assert by_tournament == {
            with_host.id: ["https://twitch.tv/host", "https://youtube.com/@host"],
        }
        # Tournaments with no host URLs are omitted (host detection off).
        assert without_host.id not in by_tournament

    async def test_dedupes_within_a_tournament(self, session: AsyncSession):
        tournament = Tournament(
            slug="dup",
            name="Dup",
            leaderboard_id=3,
            host_stream_urls=[
                "https://twitch.tv/host",
                "https://twitch.tv/host",
            ],
        )
        session.add(tournament)
        await session.commit()

        by_tournament = await get_host_stream_urls_by_tournament(session)
        assert by_tournament == {tournament.id: ["https://twitch.tv/host"]}

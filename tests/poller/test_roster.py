"""Tests for the poller's tournament-roster resolution."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Tournament, TournamentPlayer
from app.poller.roster import (
    ensure_seed_tournament,
    get_host_stream_urls_by_tournament,
    get_stream_urls_by_roster_row,
    get_tracked_profile_ids,
)


@pytest.fixture
def seed_config(monkeypatch: pytest.MonkeyPatch):
    """Point the seed-tournament config at a known roster."""
    monkeypatch.setattr(settings, "tracked_profile_ids", "1,2,3")
    monkeypatch.setattr(settings, "tournament_slug", "seed-cup")
    monkeypatch.setattr(settings, "tournament_name", "Seed Cup")
    monkeypatch.setattr(settings, "tournament_leaderboard_id", 3)


class TestEnsureSeedTournament:
    async def test_creates_tournament_when_none_exists(
        self, session: AsyncSession, seed_config: None
    ):
        await ensure_seed_tournament(session)

        tournament = (await session.execute(select(Tournament))).scalar_one()
        assert tournament.slug == "seed-cup"
        assert tournament.leaderboard_id == 3
        players = (await session.execute(select(TournamentPlayer))).scalars().all()
        assert sorted(p.profile_id for p in players) == [1, 2, 3]

    async def test_idempotent_when_a_tournament_exists(
        self, session: AsyncSession, seed_config: None
    ):
        session.add(Tournament(slug="existing", name="Existing", leaderboard_id=3))
        await session.commit()

        await ensure_seed_tournament(session)

        slugs = (await session.execute(select(Tournament.slug))).scalars().all()
        assert slugs == ["existing"]

    async def test_skips_when_no_tracked_profiles(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(settings, "tracked_profile_ids", "")

        await ensure_seed_tournament(session)

        assert (await session.execute(select(Tournament))).first() is None


class TestGetTrackedProfileIds:
    async def test_returns_union_across_tournaments(self, session: AsyncSession):
        first = Tournament(slug="a", name="A", leaderboard_id=3)
        first.tracked_players = [TournamentPlayer(profile_id=1), TournamentPlayer(profile_id=2)]
        second = Tournament(slug="b", name="B", leaderboard_id=4)
        second.tracked_players = [TournamentPlayer(profile_id=2), TournamentPlayer(profile_id=9)]
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
            profile_id=1, presentation={"streamUrls": ["https://twitch.tv/p1"]}
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

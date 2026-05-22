"""GET /v1/tournaments, /v1/tournaments/{slug}, and /{slug}/standings."""

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LiveMatchPlayer, MatchOutcome, MatchState
from app.routers.tournaments import _RECENT_RESULTS_LIMIT
from tests.conftest import (
    make_match,
    make_match_player,
    make_player,
    make_player_rating,
    make_tournament,
)


class TestListTournaments:
    async def test_empty_returns_empty_list(self, client: AsyncClient):
        response = await client.get("/v1/tournaments")
        assert response.status_code == 200
        assert response.json() == []

    async def test_lists_tournaments_newest_first(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("older", created_at=datetime(2026, 1, 1, tzinfo=UTC)))
        session.add(make_tournament("newer", created_at=datetime(2026, 2, 1, tzinfo=UTC)))
        await session.commit()

        items = (await client.get("/v1/tournaments")).json()
        assert [t["slug"] for t in items] == ["newer", "older"]


class TestGetTournamentDetail:
    async def test_returns_metadata(self, client: AsyncClient, session: AsyncSession):
        session.add(
            make_tournament(
                "cup",
                name="The Cup",
                leaderboard_id=3,
                start_date=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )
        await session.commit()

        body = (await client.get("/v1/tournaments/cup")).json()
        assert body["slug"] == "cup"
        assert body["name"] == "The Cup"
        assert body["leaderboard_id"] == 3
        assert body["start_date"].startswith("2026-06-01")

    async def test_unknown_slug_returns_404(self, client: AsyncClient):
        assert (await client.get("/v1/tournaments/nope")).status_code == 404


class TestTournamentStandings:
    async def test_unknown_tournament_returns_404(self, client: AsyncClient):
        assert (await client.get("/v1/tournaments/nope/standings")).status_code == 404

    async def test_empty_roster_returns_empty_envelope(
        self, client: AsyncClient, session: AsyncSession
    ):
        session.add(make_tournament("cup"))
        await session.commit()

        response = await client.get("/v1/tournaments/cup/standings")
        assert response.status_code == 200
        assert response.json() == {"last_polled_at": None, "items": []}

    async def test_sorted_by_current_rating_descending(
        self, client: AsyncClient, session: AsyncSession
    ):
        for profile_id, alias, rating in ((1, "low", 1500), (2, "high", 2500), (3, "mid", 2000)):
            player = make_player(profile_id, alias=alias)
            player.ratings.append(
                make_player_rating(profile_id, leaderboard_id=3, current_rating=rating)
            )
            session.add(player)
        session.add(make_tournament("cup", profile_ids=[1, 2, 3], leaderboard_id=3))
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        assert [row["alias"] for row in items] == ["high", "mid", "low"]
        assert [row["current_rating"] for row in items] == [2500, 2000, 1500]

    async def test_scoped_to_tournament_roster(self, client: AsyncClient, session: AsyncSession):
        # A player with a rating on the leaderboard but outside the roster
        # must not appear in the tournament's standings.
        for profile_id in (1, 2):
            player = make_player(profile_id)
            player.ratings.append(
                make_player_rating(profile_id, leaderboard_id=3, current_rating=2000)
            )
            session.add(player)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        assert [row["profile_id"] for row in items] == [1]

    async def test_scoped_to_tournament_leaderboard(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1, alias="solo")
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        player.ratings.append(make_player_rating(1, leaderboard_id=4, current_rating=1500))
        session.add(player)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=4))
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        assert len(items) == 1
        assert items[0]["current_rating"] == 1500

    async def test_row_has_denormalized_player_fields(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(199325, alias="Hera", country="ca")
        player.ratings.append(make_player_rating(199325, leaderboard_id=3, current_rating=2788))
        session.add(player)
        session.add(make_tournament("cup", profile_ids=[199325], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["profile_id"] == 199325
        assert row["alias"] == "Hera"
        assert row["country"] == "ca"
        assert row["current_rating"] == 2788

    async def test_cache_control_header(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("cup"))
        await session.commit()
        response = await client.get("/v1/tournaments/cup/standings")
        assert response.headers["Cache-Control"] == "public, max-age=15"


class TestStandingsRecentResults:
    """recent_results: per-player recent win/loss form on the standings row."""

    async def test_most_recent_first(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1, alias="hera")
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        for match_id, day, outcome in (
            (101, 1, MatchOutcome.LOSS),
            (102, 2, MatchOutcome.LOSS),
            (103, 3, MatchOutcome.WIN),
        ):
            match = make_match(
                match_id,
                leaderboard_id=3,
                started_at=datetime(2026, 5, day, 12, 0, tzinfo=UTC),
            )
            match.players.append(make_match_player(match_id, profile_id=1, outcome=outcome))
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["recent_results"] == ["win", "loss", "loss"]

    async def test_capped_at_limit_keeping_most_recent(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        # One match per day; the oldest (day 1) is the only LOSS. With more
        # matches than the cap, that oldest row must fall outside the window.
        total = _RECENT_RESULTS_LIMIT + 5
        for day in range(1, total + 1):
            outcome = MatchOutcome.LOSS if day == 1 else MatchOutcome.WIN
            match = make_match(
                100 + day,
                leaderboard_id=3,
                started_at=datetime(2026, 5, day, 12, 0, tzinfo=UTC),
            )
            match.players.append(make_match_player(100 + day, profile_id=1, outcome=outcome))
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        results = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0][
            "recent_results"
        ]
        assert len(results) == _RECENT_RESULTS_LIMIT
        assert results == ["win"] * _RECENT_RESULTS_LIMIT

    async def test_scoped_to_tournament_leaderboard(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)

        on_lb3 = make_match(201, leaderboard_id=3)
        on_lb3.players.append(make_match_player(201, profile_id=1, outcome=MatchOutcome.WIN))
        on_lb4 = make_match(202, leaderboard_id=4)
        on_lb4.players.append(make_match_player(202, profile_id=1, outcome=MatchOutcome.LOSS))
        no_lb = make_match(203, leaderboard_id=None)
        no_lb.players.append(make_match_player(203, profile_id=1, outcome=MatchOutcome.LOSS))
        session.add_all([on_lb3, on_lb4, no_lb])
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["recent_results"] == ["win"]

    async def test_excludes_in_progress_matches(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)

        completed = make_match(
            301, leaderboard_id=3, started_at=datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        )
        completed.players.append(make_match_player(301, profile_id=1, outcome=MatchOutcome.WIN))
        # More recent, but still in progress — null outcome, must be skipped.
        live = make_match(
            302,
            leaderboard_id=3,
            state=MatchState.IN_PROGRESS,
            completed_at=None,
            started_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
        )
        live.players.append(make_match_player(302, profile_id=1, outcome=None))
        session.add_all([completed, live])
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["recent_results"] == ["win"]

    async def test_empty_when_player_has_no_matches(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["recent_results"] == []

    async def test_per_player_buckets_are_isolated(
        self, client: AsyncClient, session: AsyncSession
    ):
        one = make_player(1, alias="one")
        one.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2200))
        two = make_player(2, alias="two")
        two.ratings.append(make_player_rating(2, leaderboard_id=3, current_rating=2100))
        session.add_all([one, two])

        for match_id, day, profile_id, outcome in (
            (401, 1, 1, MatchOutcome.LOSS),
            (402, 2, 1, MatchOutcome.WIN),
            (403, 1, 2, MatchOutcome.WIN),
        ):
            match = make_match(
                match_id,
                leaderboard_id=3,
                started_at=datetime(2026, 5, day, 12, 0, tzinfo=UTC),
            )
            match.players.append(
                make_match_player(match_id, profile_id=profile_id, outcome=outcome)
            )
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3))
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        results_by_profile = {row["profile_id"]: row["recent_results"] for row in items}
        assert results_by_profile == {1: ["win", "loss"], 2: ["win"]}


class TestStandingsInMatch:
    """in_match / live_match_id: live-match status on the standings row."""

    async def test_in_match_true_with_live_match_id(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        session.add(make_match(501, state=MatchState.IN_PROGRESS, completed_at=None))
        session.add(LiveMatchPlayer(match_id=501, profile_id=1))
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["in_match"] is True
        assert row["live_match_id"] == 501

    async def test_not_in_match_when_no_live_row(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["in_match"] is False
        assert row["live_match_id"] is None

    async def test_staging_match_counts_as_in_match(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        session.add(make_match(502, state=MatchState.STAGING, completed_at=None))
        session.add(LiveMatchPlayer(match_id=502, profile_id=1))
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["in_match"] is True
        assert row["live_match_id"] == 502

    async def test_completed_match_does_not_count(self, client: AsyncClient, session: AsyncSession):
        # A stale live_match_players row pointing at a match the recent feed
        # has already flipped to completed must not show in_match.
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        session.add(make_match(503, state=MatchState.COMPLETED))
        session.add(LiveMatchPlayer(match_id=503, profile_id=1))
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["in_match"] is False
        assert row["live_match_id"] is None

    async def test_in_match_is_per_player(self, client: AsyncClient, session: AsyncSession):
        one = make_player(1, alias="one")
        one.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2200))
        two = make_player(2, alias="two")
        two.ratings.append(make_player_rating(2, leaderboard_id=3, current_rating=2100))
        session.add_all([one, two])
        session.add(make_match(504, state=MatchState.IN_PROGRESS, completed_at=None))
        session.add(LiveMatchPlayer(match_id=504, profile_id=1))
        session.add(make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3))
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        status = {r["profile_id"]: (r["in_match"], r["live_match_id"]) for r in items}
        assert status == {1: (True, 504), 2: (False, None)}

"""GET /v1/tournaments, /v1/tournaments/{slug}, and /{slug}/standings."""

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    LiveMatchPlayer,
    MatchOutcome,
    MatchState,
    Team,
    TeamMember,
    Tournament,
    TournamentOwner,
    TournamentPlayer,
)
from app.routers.tournaments import _RECENT_RESULTS_LIMIT
from tests.conftest import (
    DEFAULT_TEST_USER_ID,
    make_match,
    make_match_player,
    make_player,
    make_player_rating,
    make_team,
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

    async def test_grand_finals_date_round_trips(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("cup", grand_finals_date=datetime(2026, 6, 15, 18, tzinfo=UTC)))
        await session.commit()

        body = (await client.get("/v1/tournaments/cup")).json()
        assert body["grand_finals_date"].startswith("2026-06-15T18")

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


class TestStandingsTournamentRecord:
    """tournament_record: games / wins / losses / streak within the window."""

    async def test_counts_wins_losses_and_games(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        for match_id, outcome in (
            (1, MatchOutcome.WIN),
            (2, MatchOutcome.WIN),
            (3, MatchOutcome.LOSS),
        ):
            match = make_match(match_id, leaderboard_id=3)
            match.players.append(make_match_player(match_id, profile_id=1, outcome=outcome))
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["tournament_record"]["games_played"] == 3
        assert row["tournament_record"]["wins"] == 2
        assert row["tournament_record"]["losses"] == 1

    async def test_win_streak_is_positive(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        # Oldest -> newest: LOSS, WIN, WIN — current streak is +2.
        for match_id, day, outcome in (
            (1, 1, MatchOutcome.LOSS),
            (2, 2, MatchOutcome.WIN),
            (3, 3, MatchOutcome.WIN),
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
        assert row["tournament_record"]["streak"] == 2

    async def test_loss_streak_is_negative(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        # Oldest -> newest: WIN, LOSS, LOSS — current streak is -2.
        for match_id, day, outcome in (
            (1, 1, MatchOutcome.WIN),
            (2, 2, MatchOutcome.LOSS),
            (3, 3, MatchOutcome.LOSS),
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
        assert row["tournament_record"]["streak"] == -2

    async def test_excludes_matches_outside_the_window(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        for match_id, day in ((1, 3), (2, 7), (3, 12)):
            match = make_match(
                match_id,
                leaderboard_id=3,
                started_at=datetime(2026, 5, day, 12, 0, tzinfo=UTC),
            )
            match.players.append(
                make_match_player(match_id, profile_id=1, outcome=MatchOutcome.WIN)
            )
            session.add(match)
        session.add(
            make_tournament(
                "cup",
                profile_ids=[1],
                leaderboard_id=3,
                start_date=datetime(2026, 5, 5, tzinfo=UTC),
                end_date=datetime(2026, 5, 10, tzinfo=UTC),
            )
        )
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["tournament_record"]["games_played"] == 1

    async def test_null_window_counts_all_matches(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        for match_id in (1, 2):
            match = make_match(match_id, leaderboard_id=3)
            match.players.append(
                make_match_player(match_id, profile_id=1, outcome=MatchOutcome.WIN)
            )
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["tournament_record"]["games_played"] == 2

    async def test_zero_record_when_no_matches(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["tournament_record"] == {
            "games_played": 0,
            "wins": 0,
            "losses": 0,
            "streak": 0,
        }


class TestUpdateTournament:
    """PATCH /v1/tournaments/{slug} — owner-gated metadata edits."""

    async def test_owner_updates_name(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", name="Old Name", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch("/v1/tournaments/cup", json={"name": "New Name"})
        assert response.status_code == 200
        assert response.json()["name"] == "New Name"
        # The change is persisted, not just echoed back.
        assert (await client.get("/v1/tournaments/cup")).json()["name"] == "New Name"

    async def test_partial_update_leaves_other_fields_untouched(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(
            make_tournament(
                "cup", name="Keep Me", leaderboard_id=3, owner_ids=[DEFAULT_TEST_USER_ID]
            )
        )
        await session.commit()

        response = await client.patch("/v1/tournaments/cup", json={"leaderboard_id": 4})
        assert response.status_code == 200
        body = response.json()
        assert body["leaderboard_id"] == 4
        assert body["name"] == "Keep Me"

    async def test_can_clear_a_date_with_null(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(
            make_tournament(
                "cup",
                start_date=datetime(2026, 6, 1, tzinfo=UTC),
                owner_ids=[DEFAULT_TEST_USER_ID],
            )
        )
        await session.commit()

        response = await client.patch("/v1/tournaments/cup", json={"start_date": None})
        assert response.status_code == 200
        assert response.json()["start_date"] is None

    async def test_can_set_grand_finals_date(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch(
            "/v1/tournaments/cup",
            json={"grand_finals_date": "2026-06-15T18:00:00Z"},
        )
        assert response.status_code == 200
        assert response.json()["grand_finals_date"].startswith("2026-06-15T18")

    async def test_can_clear_grand_finals_date_with_null(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(
            make_tournament(
                "cup",
                grand_finals_date=datetime(2026, 6, 15, 18, tzinfo=UTC),
                owner_ids=[DEFAULT_TEST_USER_ID],
            )
        )
        await session.commit()

        response = await client.patch("/v1/tournaments/cup", json={"grand_finals_date": None})
        assert response.status_code == 200
        assert response.json()["grand_finals_date"] is None

    async def test_empty_body_is_a_noop(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", name="Unchanged", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch("/v1/tournaments/cup", json={})
        assert response.status_code == 200
        assert response.json()["name"] == "Unchanged"

    async def test_start_after_end_is_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch(
            "/v1/tournaments/cup",
            json={
                "start_date": "2026-07-01T00:00:00Z",
                "end_date": "2026-06-01T00:00:00Z",
            },
        )
        assert response.status_code == 422

    async def test_explicit_null_name_is_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch("/v1/tournaments/cup", json={"name": None})
        assert response.status_code == 422


class TestCreateTournament:
    """POST /v1/tournaments — self-serve create, caller becomes owner."""

    _BODY = {
        "slug": "spring-cup",
        "name": "Spring Cup",
        "leaderboard_id": 3,
    }

    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        response = await client.post("/v1/tournaments", json=self._BODY)
        assert response.status_code == 401

    async def test_creates_tournament_and_returns_201(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.post("/v1/tournaments", json=self._BODY)
        assert response.status_code == 201
        body = response.json()
        assert body["slug"] == "spring-cup"
        assert body["name"] == "Spring Cup"
        assert body["leaderboard_id"] == 3
        # Persisted, not just echoed back.
        get_response = await client.get("/v1/tournaments/spring-cup")
        assert get_response.status_code == 200
        assert get_response.json()["slug"] == "spring-cup"

    async def test_creator_becomes_owner(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        await client.post("/v1/tournaments", json=self._BODY)
        # The caller can immediately PATCH the new tournament — proves
        # the owner row landed under their user id.
        response = await client.patch("/v1/tournaments/spring-cup", json={"name": "Renamed"})
        assert response.status_code == 200
        assert response.json()["name"] == "Renamed"

    async def test_optional_dates_round_trip(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.post(
            "/v1/tournaments",
            json={
                **self._BODY,
                "start_date": "2026-06-01T00:00:00Z",
                "end_date": "2026-06-30T23:59:59Z",
                "grand_finals_date": "2026-06-15T18:00:00Z",
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["start_date"].startswith("2026-06-01")
        assert body["end_date"].startswith("2026-06-30")
        assert body["grand_finals_date"].startswith("2026-06-15T18")

    async def test_duplicate_slug_returns_409(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("spring-cup"))
        await session.commit()

        response = await client.post("/v1/tournaments", json=self._BODY)
        assert response.status_code == 409

    async def test_invalid_slug_format_returns_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        # Uppercase, spaces, leading/trailing hyphens are all rejected.
        for bad_slug in ("Spring Cup", "-leading", "trailing-", "Caps"):
            response = await client.post("/v1/tournaments", json={**self._BODY, "slug": bad_slug})
            assert response.status_code == 422, f"expected 422 for slug={bad_slug!r}"

    async def test_start_after_end_returns_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.post(
            "/v1/tournaments",
            json={
                **self._BODY,
                "start_date": "2026-07-01T00:00:00Z",
                "end_date": "2026-06-01T00:00:00Z",
            },
        )
        assert response.status_code == 422


class TestDeleteTournament:
    """DELETE /v1/tournaments/{slug} — owner-gated, cascades to scoped rows."""

    async def test_unauthenticated_returns_401(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("cup"))
        await session.commit()

        response = await client.delete("/v1/tournaments/cup")
        assert response.status_code == 401

    async def test_unknown_slug_returns_404(self, client: AsyncClient, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.delete("/v1/tournaments/nope")
        assert response.status_code == 404

    async def test_non_owner_returns_403(self, client: AsyncClient, session: AsyncSession, auth_as):
        other_user = "00000000-0000-0000-0000-0000000000bb"
        session.add(make_tournament("cup", owner_ids=[other_user]))
        await session.commit()

        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.delete("/v1/tournaments/cup")
        assert response.status_code == 403

    async def test_owner_deletes(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.delete("/v1/tournaments/cup")
        assert response.status_code == 204
        assert response.content == b""
        assert (await client.get("/v1/tournaments/cup")).status_code == 404

    async def test_cascades_to_roster_teams_and_owners(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # A tournament with a roster, teams (with members), and a second
        # owner — every scoped row should be gone after the delete.
        tournament = make_tournament(
            "cup",
            profile_ids=[1, 2],
            owner_ids=[DEFAULT_TEST_USER_ID, "00000000-0000-0000-0000-0000000000bb"],
        )
        team = make_team("Reds", profile_ids=[1])
        tournament.teams = [team]
        session.add(tournament)
        await session.commit()

        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.delete("/v1/tournaments/cup")
        assert response.status_code == 204

        # Tournament row gone — every scoped row cascades with it.
        assert (
            await session.execute(select(Tournament).where(Tournament.slug == "cup"))
        ).scalar_one_or_none() is None
        assert (await session.execute(select(TournamentPlayer))).first() is None
        assert (await session.execute(select(Team))).first() is None
        assert (await session.execute(select(TeamMember))).first() is None
        assert (await session.execute(select(TournamentOwner))).first() is None

"""GET /v1/tournaments/{slug}/teams/standings."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import make_player, make_player_rating, make_team, make_tournament


class TestTeamStandings:
    async def test_unknown_tournament_returns_404(self, client: AsyncClient):
        assert (await client.get("/v1/tournaments/nope/teams/standings")).status_code == 404

    async def test_no_teams_returns_empty_envelope(
        self, client: AsyncClient, session: AsyncSession
    ):
        session.add(make_tournament("cup"))
        await session.commit()
        response = await client.get("/v1/tournaments/cup/teams/standings")
        assert response.status_code == 200
        assert response.json() == {"last_polled_at": None, "items": []}

    async def test_combined_rating_sum_and_average(
        self, client: AsyncClient, session: AsyncSession
    ):
        for profile_id, rating in ((1, 2000), (2, 2400)):
            player = make_player(profile_id)
            player.ratings.append(
                make_player_rating(profile_id, leaderboard_id=3, current_rating=rating)
            )
            session.add(player)
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        tournament.teams = [make_team("Red", profile_ids=[1, 2])]
        session.add(tournament)
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0]
        assert row["name"] == "Red"
        assert row["member_count"] == 2
        assert row["combined_rating_sum"] == 4400
        assert row["combined_rating_average"] == 2200.0
        # Members are sorted by rating desc.
        assert [m["profile_id"] for m in row["members"]] == [2, 1]

    async def test_sorted_by_combined_sum_descending(
        self, client: AsyncClient, session: AsyncSession
    ):
        for profile_id, rating in ((1, 1000), (2, 1000), (3, 2000), (4, 2000)):
            player = make_player(profile_id)
            player.ratings.append(
                make_player_rating(profile_id, leaderboard_id=3, current_rating=rating)
            )
            session.add(player)
        tournament = make_tournament("cup", profile_ids=[1, 2, 3, 4], leaderboard_id=3)
        tournament.teams = [
            make_team("Weak", profile_ids=[1, 2]),
            make_team("Strong", profile_ids=[3, 4]),
        ]
        session.add(tournament)
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"]
        assert [t["name"] for t in items] == ["Strong", "Weak"]

    async def test_empty_team_appears_with_zero_aggregate(
        self, client: AsyncClient, session: AsyncSession
    ):
        tournament = make_tournament("cup", leaderboard_id=3)
        tournament.teams = [make_team("Ghosts")]
        session.add(tournament)
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0]
        assert row["member_count"] == 0
        assert row["combined_rating_sum"] == 0
        assert row["combined_rating_average"] == 0.0
        assert row["members"] == []

    async def test_member_without_leaderboard_rating_is_omitted(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Player 1 has a rating; player 2 has none on the tournament's leaderboard.
        rated = make_player(1)
        rated.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(rated)
        session.add(make_player(2))
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        tournament.teams = [make_team("Red", profile_ids=[1, 2])]
        session.add(tournament)
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0]
        assert row["member_count"] == 1
        assert [m["profile_id"] for m in row["members"]] == [1]

    async def test_cache_control_header(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("cup"))
        await session.commit()
        response = await client.get("/v1/tournaments/cup/teams/standings")
        assert response.headers["Cache-Control"] == "public, max-age=15"

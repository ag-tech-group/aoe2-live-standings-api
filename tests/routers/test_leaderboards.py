"""GET /v1/leaderboards and GET /v1/leaderboards/{leaderboard_id}/standings."""

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app import leaderboards_cache
from app.schemas.leaderboard import LeaderboardRead
from tests.conftest import make_player, make_player_rating


class TestListLeaderboards:
    async def test_empty_cache_returns_empty_envelope(self, client: AsyncClient):
        response = await client.get("/v1/leaderboards")
        assert response.status_code == 200
        assert response.json() == {"last_polled_at": None, "items": []}

    async def test_populated_cache_returns_metadata(self, client: AsyncClient):
        leaderboards_cache.set_cache(
            [
                LeaderboardRead(leaderboard_id=3, name="1v1 RM Ranked", is_ranked=True),
                LeaderboardRead(leaderboard_id=4, name="Team RM Ranked", is_ranked=True),
            ],
            refreshed_at=datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC),
        )

        response = await client.get("/v1/leaderboards")
        payload = response.json()

        assert response.status_code == 200
        assert payload["last_polled_at"] is not None
        assert [lb["leaderboard_id"] for lb in payload["items"]] == [3, 4]

    async def test_cache_control_header(self, client: AsyncClient):
        response = await client.get("/v1/leaderboards")
        assert response.headers["Cache-Control"] == "public, max-age=15"


class TestGetStandings:
    async def test_empty_db_returns_empty_envelope(self, client: AsyncClient):
        response = await client.get("/v1/leaderboards/3/standings")
        assert response.status_code == 200
        assert response.json() == {"last_polled_at": None, "items": []}

    async def test_sorted_by_current_rating_descending(
        self, client: AsyncClient, session: AsyncSession
    ):
        for profile_id, alias, rating in (
            (1, "low", 1500),
            (2, "high", 2500),
            (3, "mid", 2000),
        ):
            player = make_player(profile_id, alias=alias)
            player.ratings.append(
                make_player_rating(profile_id, leaderboard_id=3, current_rating=rating)
            )
            session.add(player)
        await session.commit()

        response = await client.get("/v1/leaderboards/3/standings")
        items = response.json()["items"]

        assert [row["alias"] for row in items] == ["high", "mid", "low"]
        assert [row["current_rating"] for row in items] == [2500, 2000, 1500]

    async def test_filters_to_requested_leaderboard(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1, alias="solo")
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        player.ratings.append(make_player_rating(1, leaderboard_id=4, current_rating=1500))
        session.add(player)
        await session.commit()

        response = await client.get("/v1/leaderboards/4/standings")
        items = response.json()["items"]

        assert len(items) == 1
        assert items[0]["current_rating"] == 1500

    async def test_standings_row_has_denormalized_player_fields(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(199325, alias="Hera", country="ca")
        player.ratings.append(make_player_rating(199325, leaderboard_id=3, current_rating=2788))
        session.add(player)
        await session.commit()

        row = (await client.get("/v1/leaderboards/3/standings")).json()["items"][0]

        assert row["profile_id"] == 199325
        assert row["alias"] == "Hera"
        assert row["country"] == "ca"
        assert row["current_rating"] == 2788

    async def test_cache_control_header(self, client: AsyncClient):
        response = await client.get("/v1/leaderboards/3/standings")
        assert response.headers["Cache-Control"] == "public, max-age=15"

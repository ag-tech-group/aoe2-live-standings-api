"""GET /v1/leaderboards."""

from datetime import UTC, datetime

from httpx import AsyncClient

from app import leaderboards_cache
from app.schemas.leaderboard import LeaderboardRead


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

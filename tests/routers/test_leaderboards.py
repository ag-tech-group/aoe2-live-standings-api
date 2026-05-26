"""GET /v1/leaderboards."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Leaderboard


class TestListLeaderboards:
    async def test_empty_table_returns_empty_envelope(self, client: AsyncClient):
        response = await client.get("/v1/leaderboards")
        assert response.status_code == 200
        assert response.json() == {"last_polled_at": None, "items": []}

    async def test_populated_table_returns_metadata(
        self, client: AsyncClient, session: AsyncSession
    ):
        session.add_all(
            [
                Leaderboard(leaderboard_id=3, name="1v1 RM Ranked", is_ranked=True, matchtypes=[6]),
                Leaderboard(
                    leaderboard_id=4, name="Team RM Ranked", is_ranked=True, matchtypes=[7, 8]
                ),
            ]
        )
        await session.commit()

        response = await client.get("/v1/leaderboards")
        payload = response.json()

        assert response.status_code == 200
        assert payload["last_polled_at"] is not None
        assert [lb["leaderboard_id"] for lb in payload["items"]] == [3, 4]

    async def test_cache_control_header(self, client: AsyncClient):
        # Same split as standings (#96) — see test_tournaments.py for the why.
        response = await client.get("/v1/leaderboards")
        assert (
            response.headers["Cache-Control"] == "public, s-maxage=15, max-age=0, must-revalidate"
        )

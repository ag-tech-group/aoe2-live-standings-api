"""GET /v1/civilizations."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Civilization


class TestListCivilizations:
    async def test_empty_table_returns_empty_envelope(self, client: AsyncClient):
        response = await client.get("/v1/civilizations")
        assert response.status_code == 200
        assert response.json() == {"last_polled_at": None, "items": []}

    async def test_populated_table_returns_metadata(
        self, client: AsyncClient, session: AsyncSession
    ):
        session.add_all(
            [
                Civilization(civilization_id=7, name="Burgundians"),
                Civilization(civilization_id=0, name="Armenians"),
            ]
        )
        await session.commit()

        payload = (await client.get("/v1/civilizations")).json()
        assert payload["last_polled_at"] is not None
        # Ordered by civilization_id.
        assert payload["items"] == [
            {"civilization_id": 0, "name": "Armenians"},
            {"civilization_id": 7, "name": "Burgundians"},
        ]

    async def test_cache_control_header(self, client: AsyncClient):
        # Same split as /v1/leaderboards (#96).
        response = await client.get("/v1/civilizations")
        assert (
            response.headers["Cache-Control"] == "public, s-maxage=15, max-age=0, must-revalidate"
        )

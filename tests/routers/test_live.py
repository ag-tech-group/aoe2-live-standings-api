"""GET /v1/live."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MatchState
from tests.conftest import make_match, make_match_player


class TestGetLive:
    async def test_empty_db_returns_empty_envelope(self, client: AsyncClient):
        response = await client.get("/v1/live")
        assert response.status_code == 200
        assert response.json() == {"last_polled_at": None, "items": []}

    async def test_returns_only_staging_and_in_progress(
        self, client: AsyncClient, session: AsyncSession
    ):
        for match_id, state in (
            (1, MatchState.COMPLETED),
            (2, MatchState.IN_PROGRESS),
            (3, MatchState.STAGING),
            (4, MatchState.COMPLETED),
        ):
            completed_at = None if state != MatchState.COMPLETED else make_match(0).completed_at
            match = make_match(match_id, state=state, completed_at=completed_at)
            match.players.append(make_match_player(match_id, profile_id=99))
            session.add(match)
        await session.commit()

        ids = sorted(m["match_id"] for m in (await client.get("/v1/live")).json()["items"])
        assert ids == [2, 3]

    async def test_each_item_includes_players(self, client: AsyncClient, session: AsyncSession):
        match = make_match(1, state=MatchState.IN_PROGRESS, completed_at=None)
        match.players.append(make_match_player(1, profile_id=10))
        match.players.append(make_match_player(1, profile_id=11))
        session.add(match)
        await session.commit()

        item = (await client.get("/v1/live")).json()["items"][0]
        assert sorted(p["profile_id"] for p in item["players"]) == [10, 11]

    async def test_cache_control_header(self, client: AsyncClient):
        response = await client.get("/v1/live")
        assert response.headers["Cache-Control"] == "public, max-age=10"

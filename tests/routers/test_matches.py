"""GET /v1/matches and GET /v1/matches/{match_id}."""

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MatchState
from tests.conftest import make_match, make_match_player


class TestListMatches:
    async def test_empty_db_returns_empty_envelope(self, client: AsyncClient):
        response = await client.get("/v1/matches")
        assert response.status_code == 200
        assert response.json() == {"last_polled_at": None, "items": []}

    async def test_sorted_by_started_at_descending(
        self, client: AsyncClient, session: AsyncSession
    ):
        for match_id, started_hour in ((1, 9), (2, 11), (3, 10)):
            match = make_match(
                match_id,
                started_at=datetime(2026, 5, 18, started_hour, 0, 0, tzinfo=UTC),
            )
            match.players.append(make_match_player(match_id, profile_id=99))
            session.add(match)
        await session.commit()

        ids = [m["match_id"] for m in (await client.get("/v1/matches")).json()["items"]]
        assert ids == [2, 3, 1]

    async def test_each_item_includes_players(self, client: AsyncClient, session: AsyncSession):
        match = make_match(1)
        match.players.append(make_match_player(1, profile_id=10))
        match.players.append(make_match_player(1, profile_id=11))
        session.add(match)
        await session.commit()

        item = (await client.get("/v1/matches")).json()["items"][0]
        assert sorted(p["profile_id"] for p in item["players"]) == [10, 11]

    async def test_profile_id_filter(self, client: AsyncClient, session: AsyncSession):
        for match_id, profile_id in ((1, 100), (2, 200), (3, 100)):
            match = make_match(match_id)
            match.players.append(make_match_player(match_id, profile_id=profile_id))
            session.add(match)
        await session.commit()

        items = (await client.get("/v1/matches", params={"profile_id": 100})).json()["items"]
        assert sorted(m["match_id"] for m in items) == [1, 3]

    async def test_leaderboard_id_filter(self, client: AsyncClient, session: AsyncSession):
        for match_id, lb in ((1, 3), (2, 4), (3, 3)):
            match = make_match(match_id, leaderboard_id=lb)
            match.players.append(make_match_player(match_id, profile_id=99))
            session.add(match)
        await session.commit()

        items = (await client.get("/v1/matches", params={"leaderboard_id": 3})).json()["items"]
        assert sorted(m["match_id"] for m in items) == [1, 3]

    async def test_state_filter(self, client: AsyncClient, session: AsyncSession):
        match_completed = make_match(1, state=MatchState.COMPLETED)
        match_in_progress = make_match(2, state=MatchState.IN_PROGRESS, completed_at=None)
        match_staging = make_match(3, state=MatchState.STAGING, completed_at=None)
        for m in (match_completed, match_in_progress, match_staging):
            m.players.append(make_match_player(m.match_id, profile_id=99))
            session.add(m)
        await session.commit()

        items = (await client.get("/v1/matches", params={"state": "in_progress"})).json()["items"]
        assert [m["match_id"] for m in items] == [2]

    async def test_limit_caps_returned_items(self, client: AsyncClient, session: AsyncSession):
        for match_id in range(5):
            match = make_match(match_id)
            match.players.append(make_match_player(match_id, profile_id=99))
            session.add(match)
        await session.commit()

        items = (await client.get("/v1/matches", params={"limit": 2})).json()["items"]
        assert len(items) == 2

    async def test_limit_out_of_range_returns_422(self, client: AsyncClient):
        response = await client.get("/v1/matches", params={"limit": 500})
        assert response.status_code == 422


class TestGetMatch:
    async def test_unknown_match_returns_404(self, client: AsyncClient):
        response = await client.get("/v1/matches/99999")
        assert response.status_code == 404

    async def test_returns_match_with_all_players(self, client: AsyncClient, session: AsyncSession):
        match = make_match(309483878)
        match.players.append(make_match_player(309483878, profile_id=199325))
        match.players.append(make_match_player(309483878, profile_id=409748))
        session.add(match)
        await session.commit()

        payload = (await client.get("/v1/matches/309483878")).json()
        assert payload["match_id"] == 309483878
        assert payload["last_polled_at"] is not None
        assert sorted(p["profile_id"] for p in payload["players"]) == [199325, 409748]

    async def test_completed_match_uses_60s_cache(self, client: AsyncClient, session: AsyncSession):
        match = make_match(1, state=MatchState.COMPLETED)
        match.players.append(make_match_player(1, profile_id=99))
        session.add(match)
        await session.commit()

        response = await client.get("/v1/matches/1")
        assert response.headers["Cache-Control"] == "public, max-age=60"

    async def test_in_progress_match_uses_no_store(
        self, client: AsyncClient, session: AsyncSession
    ):
        match = make_match(1, state=MatchState.IN_PROGRESS, completed_at=None)
        match.players.append(make_match_player(1, profile_id=99))
        session.add(match)
        await session.commit()

        response = await client.get("/v1/matches/1")
        assert response.headers["Cache-Control"] == "no-store"

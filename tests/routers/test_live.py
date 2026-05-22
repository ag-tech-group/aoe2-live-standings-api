"""GET /v1/tournaments/{slug}/live."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LiveMatchPlayer, MatchState
from tests.conftest import make_match, make_tournament


class TestTournamentLive:
    async def test_unknown_tournament_returns_404(self, client: AsyncClient):
        assert (await client.get("/v1/tournaments/nope/live")).status_code == 404

    async def test_empty_returns_empty_envelope(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("cup"))
        await session.commit()
        response = await client.get("/v1/tournaments/cup/live")
        assert response.status_code == 200
        assert response.json() == {"last_polled_at": None, "items": []}

    async def test_returns_only_roster_live_matches(
        self, client: AsyncClient, session: AsyncSession
    ):
        # A roster member's live match shows; a live match with no roster
        # member does not.
        session.add(make_match(1, state=MatchState.IN_PROGRESS, completed_at=None))
        session.add(make_match(2, state=MatchState.IN_PROGRESS, completed_at=None))
        session.add(LiveMatchPlayer(match_id=1, profile_id=10))
        session.add(LiveMatchPlayer(match_id=2, profile_id=999))
        session.add(make_tournament("cup", profile_ids=[10]))
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/live")).json()["items"]
        assert [m["match_id"] for m in items] == [1]

    async def test_excludes_completed_matches(self, client: AsyncClient, session: AsyncSession):
        # Even with a live_match_players row, a completed match is not live.
        session.add(make_match(1, state=MatchState.COMPLETED))
        session.add(LiveMatchPlayer(match_id=1, profile_id=10))
        session.add(make_tournament("cup", profile_ids=[10]))
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/live")).json()["items"]
        assert items == []

    async def test_cache_control_header(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("cup"))
        await session.commit()
        response = await client.get("/v1/tournaments/cup/live")
        assert response.headers["Cache-Control"] == "public, max-age=10"

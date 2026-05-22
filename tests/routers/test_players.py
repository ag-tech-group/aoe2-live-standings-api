"""Player + roster endpoints under /v1/tournaments/{slug}/players."""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TournamentPlayer
from app.poller.roster import get_tracked_profile_ids
from tests.conftest import (
    DEFAULT_TEST_USER_ID,
    make_match,
    make_match_player,
    make_player,
    make_player_rating,
    make_tournament,
)


class TestListPlayers:
    async def test_unknown_tournament_returns_404(self, client: AsyncClient):
        assert (await client.get("/v1/tournaments/nope/players")).status_code == 404

    async def test_empty_roster_returns_empty_envelope(
        self, client: AsyncClient, session: AsyncSession
    ):
        session.add(make_tournament("cup"))
        await session.commit()
        response = await client.get("/v1/tournaments/cup/players")
        assert response.status_code == 200
        assert response.json() == {"last_polled_at": None, "items": []}

    async def test_returns_roster_with_ratings_sorted_alphabetically(
        self, client: AsyncClient, session: AsyncSession
    ):
        hera = make_player(199325, alias="VIT | Hera")
        hera.ratings.append(make_player_rating(199325, leaderboard_id=3, current_rating=2788))
        tatoh = make_player(409748, alias="AB | TaToH")
        tatoh.ratings.append(make_player_rating(409748, leaderboard_id=3, current_rating=2454))
        session.add_all([hera, tatoh])
        session.add(make_tournament("cup", profile_ids=[199325, 409748]))
        await session.commit()

        payload = (await client.get("/v1/tournaments/cup/players")).json()
        aliases = [p["alias"] for p in payload["items"]]
        assert aliases == ["AB | TaToH", "VIT | Hera"]
        assert payload["last_polled_at"] is not None
        hera_payload = next(p for p in payload["items"] if p["profile_id"] == 199325)
        assert hera_payload["ratings"][0]["current_rating"] == 2788

    async def test_scoped_to_tournament_roster(self, client: AsyncClient, session: AsyncSession):
        # A player outside the roster is not listed.
        for profile_id in (1, 2):
            session.add(make_player(profile_id))
        session.add(make_tournament("cup", profile_ids=[1]))
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/players")).json()["items"]
        assert [p["profile_id"] for p in items] == [1]

    async def test_leaderboard_id_filters_ratings_but_keeps_player(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1, alias="solo")
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=1800))
        player.ratings.append(make_player_rating(1, leaderboard_id=4, current_rating=1600))
        session.add(player)
        session.add(make_tournament("cup", profile_ids=[1]))
        await session.commit()

        items = (
            await client.get("/v1/tournaments/cup/players", params={"leaderboard_id": 3})
        ).json()["items"]
        assert len(items) == 1
        assert len(items[0]["ratings"]) == 1
        assert items[0]["ratings"][0]["leaderboard_id"] == 3

    async def test_player_included_even_when_no_ratings_match_filter(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1, alias="solo")
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=1800))
        session.add(player)
        session.add(make_tournament("cup", profile_ids=[1]))
        await session.commit()

        items = (
            await client.get("/v1/tournaments/cup/players", params={"leaderboard_id": 99})
        ).json()["items"]
        assert len(items) == 1
        assert items[0]["ratings"] == []

    async def test_cache_control_header(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("cup"))
        await session.commit()
        response = await client.get("/v1/tournaments/cup/players")
        assert response.headers["Cache-Control"] == "public, max-age=15"


class TestGetPlayer:
    async def test_profile_outside_roster_returns_404(
        self, client: AsyncClient, session: AsyncSession
    ):
        session.add(make_player(2, alias="outsider"))
        session.add(make_tournament("cup", profile_ids=[1]))
        await session.commit()
        assert (await client.get("/v1/tournaments/cup/players/2")).status_code == 404

    async def test_returns_player_with_ratings_and_recent_matches(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1, alias="Hera")
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2788))
        session.add(player)
        for match_id in (10, 11, 12):
            match = make_match(match_id)
            match.players.append(make_match_player(match_id, profile_id=1))
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1]))
        await session.commit()

        payload = (await client.get("/v1/tournaments/cup/players/1")).json()
        assert payload["alias"] == "Hera"
        assert payload["last_polled_at"] is not None
        assert len(payload["ratings"]) == 1
        assert len(payload["recent_matches"]) == 3
        assert all("players" in m for m in payload["recent_matches"])

    async def test_match_limit_caps_recent_matches(
        self, client: AsyncClient, session: AsyncSession
    ):
        session.add(make_player(1, alias="Hera"))
        for i in range(5):
            match = make_match(100 + i)
            match.players.append(make_match_player(100 + i, profile_id=1))
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1]))
        await session.commit()

        payload = (
            await client.get("/v1/tournaments/cup/players/1", params={"match_limit": 2})
        ).json()
        assert len(payload["recent_matches"]) == 2

    async def test_match_limit_out_of_range_returns_422(
        self, client: AsyncClient, session: AsyncSession
    ):
        session.add(make_tournament("cup", profile_ids=[1]))
        await session.commit()
        response = await client.get("/v1/tournaments/cup/players/1", params={"match_limit": 999})
        assert response.status_code == 422


class TestAddRosterPlayer:
    """POST /v1/tournaments/{slug}/players — owner-gated roster add."""

    async def test_owner_adds_player(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.post("/v1/tournaments/cup/players", json={"profile_id": 199325})
        assert response.status_code == 204

        roster = (await session.execute(select(TournamentPlayer.profile_id))).scalars().all()
        assert roster == [199325]

    async def test_added_player_is_visible_to_the_poller(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # The poller re-resolves the roster every cycle, so a player added
        # over HTTP is tracked without a redeploy.
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        await client.post("/v1/tournaments/cup/players", json={"profile_id": 555})
        assert 555 in await get_tracked_profile_ids(session)

    async def test_adding_a_duplicate_is_409(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", profile_ids=[199325], owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.post("/v1/tournaments/cup/players", json={"profile_id": 199325})
        assert response.status_code == 409

    async def test_non_positive_profile_id_is_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.post("/v1/tournaments/cup/players", json={"profile_id": 0})
        assert response.status_code == 422


class TestRemoveRosterPlayer:
    """DELETE /v1/tournaments/{slug}/players/{profile_id} — owner-gated."""

    async def test_owner_removes_player(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", profile_ids=[199325], owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.delete("/v1/tournaments/cup/players/199325")
        assert response.status_code == 204
        assert (await session.execute(select(TournamentPlayer))).scalars().all() == []

    async def test_removing_a_player_not_on_the_roster_is_404(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.delete("/v1/tournaments/cup/players/199325")
        assert response.status_code == 404

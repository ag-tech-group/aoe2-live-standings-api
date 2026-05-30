"""Placeholder roster endpoints under /v1/tournaments/{slug}/placeholders."""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Tournament, TournamentPlaceholderPlayer
from tests.conftest import DEFAULT_TEST_USER_ID, make_tournament


class TestListPlaceholders:
    async def test_unknown_tournament_returns_404(self, client: AsyncClient):
        assert (await client.get("/v1/tournaments/nope/placeholders")).status_code == 404

    async def test_empty_returns_empty_envelope(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("cup"))
        await session.commit()
        response = await client.get("/v1/tournaments/cup/placeholders")
        assert response.status_code == 200
        assert response.json() == {"last_polled_at": None, "items": []}

    async def test_lists_placeholders_alphabetically(
        self, client: AsyncClient, session: AsyncSession
    ):
        tournament = make_tournament("cup")
        tournament.placeholder_players = [
            TournamentPlaceholderPlayer(name="Zeke", presentation={}),
            TournamentPlaceholderPlayer(name="Alice", presentation={"flag": "🇺🇸"}),
            TournamentPlaceholderPlayer(name="Marco", presentation={"displayName": "Marco P."}),
        ]
        session.add(tournament)
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/placeholders")).json()["items"]
        assert [p["name"] for p in items] == ["Alice", "Marco", "Zeke"]
        alice = next(p for p in items if p["name"] == "Alice")
        assert alice["presentation"] == {"flag": "🇺🇸"}


class TestAddPlaceholder:
    """POST /v1/tournaments/{slug}/placeholders — owner-gated."""

    async def test_unauthenticated_returns_401(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("cup"))
        await session.commit()
        response = await client.post("/v1/tournaments/cup/placeholders", json={"name": "iyouxin"})
        assert response.status_code == 401

    async def test_non_owner_returns_403(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup"))  # no owner
        await session.commit()
        response = await client.post("/v1/tournaments/cup/placeholders", json={"name": "iyouxin"})
        assert response.status_code == 403

    async def test_owner_adds_placeholder(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.post(
            "/v1/tournaments/cup/placeholders",
            json={"name": "iyouxin", "presentation": {"flag": "🇺🇦"}},
        )
        assert response.status_code == 204

        roster = (await session.execute(select(TournamentPlaceholderPlayer))).scalars().all()
        assert len(roster) == 1
        assert roster[0].name == "iyouxin"
        assert roster[0].presentation == {"flag": "🇺🇦"}

    async def test_presentation_defaults_to_empty(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.post("/v1/tournaments/cup/placeholders", json={"name": "iyouxin"})
        assert response.status_code == 204
        row = (await session.execute(select(TournamentPlaceholderPlayer))).scalar_one()
        assert row.presentation == {}

    async def test_duplicate_name_is_409(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.placeholder_players = [TournamentPlaceholderPlayer(name="iyouxin")]
        session.add(tournament)
        await session.commit()

        response = await client.post("/v1/tournaments/cup/placeholders", json={"name": "iyouxin"})
        assert response.status_code == 409

    async def test_empty_name_is_422(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.post("/v1/tournaments/cup/placeholders", json={"name": ""})
        assert response.status_code == 422


class TestUpdatePlaceholder:
    """PATCH /v1/tournaments/{slug}/placeholders/{name} — owner-gated."""

    async def test_owner_replaces_presentation(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.placeholder_players = [
            TournamentPlaceholderPlayer(name="iyouxin", presentation={"flag": "🇺🇦"})
        ]
        session.add(tournament)
        await session.commit()

        response = await client.patch(
            "/v1/tournaments/cup/placeholders/iyouxin",
            json={"presentation": {"displayName": "iyouxin", "flag": "🇺🇦"}},
        )
        assert response.status_code == 204

        session.expire_all()
        row = (await session.execute(select(TournamentPlaceholderPlayer))).scalar_one()
        assert row.presentation == {"displayName": "iyouxin", "flag": "🇺🇦"}

    async def test_patch_replaces_whole_bag(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.placeholder_players = [
            TournamentPlaceholderPlayer(name="iyouxin", presentation={"flag": "🇺🇦", "bio": "TBD"})
        ]
        session.add(tournament)
        await session.commit()

        await client.patch(
            "/v1/tournaments/cup/placeholders/iyouxin",
            json={"presentation": {"flag": "🇺🇦"}},
        )
        session.expire_all()
        row = (await session.execute(select(TournamentPlaceholderPlayer))).scalar_one()
        assert row.presentation == {"flag": "🇺🇦"}

    async def test_unknown_name_is_404(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch(
            "/v1/tournaments/cup/placeholders/missing", json={"presentation": {}}
        )
        assert response.status_code == 404


class TestRemovePlaceholder:
    """DELETE /v1/tournaments/{slug}/placeholders/{name} — owner-gated."""

    async def test_owner_removes_placeholder(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.placeholder_players = [TournamentPlaceholderPlayer(name="iyouxin")]
        session.add(tournament)
        await session.commit()

        response = await client.delete("/v1/tournaments/cup/placeholders/iyouxin")
        assert response.status_code == 204
        assert (await session.execute(select(TournamentPlaceholderPlayer))).scalars().all() == []

    async def test_unknown_name_is_404(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.delete("/v1/tournaments/cup/placeholders/missing")
        assert response.status_code == 404


class TestPlaceholdersCascadeOnTournamentDelete:
    async def test_deleting_tournament_clears_placeholders(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # ON DELETE CASCADE on the FK means dropping the tournament drops
        # its placeholder rows transparently.
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.placeholder_players = [TournamentPlaceholderPlayer(name="iyouxin")]
        session.add(tournament)
        await session.commit()

        await client.delete("/v1/tournaments/cup")
        assert (await session.execute(select(TournamentPlaceholderPlayer))).scalars().all() == []
        assert (await session.execute(select(Tournament))).scalars().all() == []

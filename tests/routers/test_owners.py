"""GET/POST/DELETE on /v1/tournaments/{slug}/owners — role-management API."""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.users_client import UserIdentity
from app.models import TournamentOwner
from tests.conftest import DEFAULT_TEST_USER_ID, make_tournament

# Stand-in for a second criticalbit user — useful for "grant another
# user", "non-owner is forbidden", and "two-owner tournament" cases.
OTHER_USER_ID = "00000000-0000-0000-0000-0000000000bb"


class TestListTournamentOwners:
    """GET /v1/tournaments/{slug}/owners — owner-gated."""

    async def test_unauthenticated_returns_401(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.get("/v1/tournaments/cup/owners")
        assert response.status_code == 401

    async def test_unknown_tournament_returns_404(self, client: AsyncClient, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.get("/v1/tournaments/nope/owners")
        assert response.status_code == 404

    async def test_non_owner_returns_403(self, client: AsyncClient, session: AsyncSession, auth_as):
        session.add(make_tournament("cup", owner_ids=[OTHER_USER_ID]))
        await session.commit()

        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.get("/v1/tournaments/cup/owners")
        assert response.status_code == 403

    async def test_owner_lists_owners_oldest_first(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        # Force distinct created_at so the ordering is deterministic.
        tournament.owners[0].created_at = datetime(2026, 5, 1, tzinfo=UTC)
        tournament.owners.append(
            TournamentOwner(user_id=OTHER_USER_ID, created_at=datetime(2026, 5, 2, tzinfo=UTC))
        )
        session.add(tournament)
        await session.commit()

        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.get("/v1/tournaments/cup/owners")
        assert response.status_code == 200
        body = response.json()
        assert [o["user_id"] for o in body] == [DEFAULT_TEST_USER_ID, OTHER_USER_ID]
        assert all("created_at" in o for o in body)

    async def test_cache_control_is_no_store(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # Admin-only, must reflect grant/revoke mutations immediately.
        # Matches the #103 middleware default (`no-store`), but declared
        # explicitly to document the deliberate no-cache posture.
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.get("/v1/tournaments/cup/owners")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"


class TestGrantTournamentOwner:
    """POST /v1/tournaments/{slug}/owners — owner-gated."""

    async def test_unauthenticated_returns_401(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.post("/v1/tournaments/cup/owners", json={"user_id": OTHER_USER_ID})
        assert response.status_code == 401

    async def test_unknown_tournament_returns_404(self, client: AsyncClient, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.post("/v1/tournaments/nope/owners", json={"user_id": OTHER_USER_ID})
        assert response.status_code == 404

    async def test_non_owner_returns_403(self, client: AsyncClient, session: AsyncSession, auth_as):
        session.add(make_tournament("cup", owner_ids=[OTHER_USER_ID]))
        await session.commit()

        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.post(
            "/v1/tournaments/cup/owners", json={"user_id": DEFAULT_TEST_USER_ID}
        )
        assert response.status_code == 403

    async def test_owner_grants_ownership(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.post("/v1/tournaments/cup/owners", json={"user_id": OTHER_USER_ID})
        assert response.status_code == 204

        # Visible in the listing now.
        listed = (await client.get("/v1/tournaments/cup/owners")).json()
        assert OTHER_USER_ID in [o["user_id"] for o in listed]

    async def test_granted_user_can_immediately_patch(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # Cross-check: the grant actually wires the new user up as an
        # owner — they can use the management API right away.
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        auth_as(DEFAULT_TEST_USER_ID)
        await client.post("/v1/tournaments/cup/owners", json={"user_id": OTHER_USER_ID})

        auth_as(OTHER_USER_ID)
        response = await client.patch("/v1/tournaments/cup", json={"name": "Renamed"})
        assert response.status_code == 200
        assert response.json()["name"] == "Renamed"

    async def test_duplicate_user_returns_409(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID, OTHER_USER_ID]))
        await session.commit()

        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.post("/v1/tournaments/cup/owners", json={"user_id": OTHER_USER_ID})
        assert response.status_code == 409

    async def test_invalid_user_id_format_returns_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        auth_as(DEFAULT_TEST_USER_ID)
        for bad_id in ("not-a-uuid", "00000000-0000-0000-0000", "", "x" * 36):
            response = await client.post("/v1/tournaments/cup/owners", json={"user_id": bad_id})
            assert response.status_code == 422, f"expected 422 for user_id={bad_id!r}"


class TestRevokeTournamentOwner:
    """DELETE /v1/tournaments/{slug}/owners/{user_id} — owner-gated."""

    async def test_unauthenticated_returns_401(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID, OTHER_USER_ID]))
        await session.commit()

        response = await client.delete(f"/v1/tournaments/cup/owners/{OTHER_USER_ID}")
        assert response.status_code == 401

    async def test_unknown_tournament_returns_404(self, client: AsyncClient, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.delete(f"/v1/tournaments/nope/owners/{OTHER_USER_ID}")
        assert response.status_code == 404

    async def test_non_owner_returns_403(self, client: AsyncClient, session: AsyncSession, auth_as):
        session.add(make_tournament("cup", owner_ids=[OTHER_USER_ID]))
        await session.commit()

        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.delete(f"/v1/tournaments/cup/owners/{OTHER_USER_ID}")
        assert response.status_code == 403

    async def test_unknown_user_returns_404(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.delete(f"/v1/tournaments/cup/owners/{OTHER_USER_ID}")
        assert response.status_code == 404

    async def test_owner_revokes_other_owner(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID, OTHER_USER_ID]))
        await session.commit()

        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.delete(f"/v1/tournaments/cup/owners/{OTHER_USER_ID}")
        assert response.status_code == 204
        assert response.content == b""

        # Revoked user can no longer manage.
        auth_as(OTHER_USER_ID)
        forbidden = await client.patch("/v1/tournaments/cup", json={"name": "x"})
        assert forbidden.status_code == 403

    async def test_cannot_remove_last_owner_returns_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.delete(f"/v1/tournaments/cup/owners/{DEFAULT_TEST_USER_ID}")
        assert response.status_code == 422

        # And the owner row is still there — the conditional DELETE didn't fire.
        rows = (await session.execute(select(TournamentOwner))).scalars().all()
        assert [r.user_id for r in rows] == [DEFAULT_TEST_USER_ID]


class TestListTournamentOwnersEnrichment:
    """Identity enrichment via auth-api /users/lookup on the owners list.

    The conftest's autouse fixture stubs ``fetch_identities`` to a no-op so
    the existing tests don't reach the network; these tests override it
    with a stub that returns canned identities to exercise the merge path.
    """

    async def test_enriched_fields_appear_in_response(
        self,
        client: AsyncClient,
        session: AsyncSession,
        auth_as,
        monkeypatch: pytest.MonkeyPatch,
    ):
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        async def _fake_fetch(user_ids, *, access_token):
            return {
                DEFAULT_TEST_USER_ID: UserIdentity(
                    user_id=DEFAULT_TEST_USER_ID,
                    email="hera@criticalbit.gg",
                    display_name="Hera",
                    avatar_url="https://example.com/hera.png",
                ),
            }

        monkeypatch.setattr("app.routers.owners.fetch_identities", _fake_fetch)

        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.get("/v1/tournaments/cup/owners")
        assert response.status_code == 200
        body = response.json()
        assert body[0]["user_id"] == DEFAULT_TEST_USER_ID
        assert body[0]["display_name"] == "Hera"
        assert body[0]["email"] == "hera@criticalbit.gg"
        assert body[0]["avatar_url"] == "https://example.com/hera.png"
        assert "created_at" in body[0]

    async def test_missing_identity_returns_null_fields(
        self,
        client: AsyncClient,
        session: AsyncSession,
        auth_as,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # auth-api returned successfully but didn't know this user (or the
        # call failed and got swallowed) — the row still resolves with
        # ``user_id`` + ``created_at`` and null enrichment fields, so the
        # admin UI degrades gracefully.
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        async def _empty_fetch(user_ids, *, access_token):
            return {}

        monkeypatch.setattr("app.routers.owners.fetch_identities", _empty_fetch)

        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.get("/v1/tournaments/cup/owners")
        assert response.status_code == 200
        row = response.json()[0]
        assert row["user_id"] == DEFAULT_TEST_USER_ID
        assert row["display_name"] is None
        assert row["email"] is None
        assert row["avatar_url"] is None

    async def test_fetch_called_with_all_owner_ids(
        self,
        client: AsyncClient,
        session: AsyncSession,
        auth_as,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # Two owners — both ids should be passed to the auth-api client in
        # a single batched call.
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.owners.append(TournamentOwner(user_id=OTHER_USER_ID))
        session.add(tournament)
        await session.commit()

        captured: dict = {"ids": None, "token": "<not-called>"}

        async def _capturing_fetch(user_ids, *, access_token):
            captured["ids"] = list(user_ids)
            captured["token"] = access_token
            return {}

        monkeypatch.setattr("app.routers.owners.fetch_identities", _capturing_fetch)

        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.get("/v1/tournaments/cup/owners")
        assert response.status_code == 200
        assert set(captured["ids"]) == {DEFAULT_TEST_USER_ID, OTHER_USER_ID}

    async def test_access_token_cookie_is_forwarded_to_client(
        self,
        client: AsyncClient,
        session: AsyncSession,
        auth_as,
        monkeypatch: pytest.MonkeyPatch,
    ):
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        captured: dict = {"token": "<not-called>"}

        async def _capturing_fetch(user_ids, *, access_token):
            captured["token"] = access_token
            return {}

        monkeypatch.setattr("app.routers.owners.fetch_identities", _capturing_fetch)

        # `auth_as` overrides the dep but doesn't set a cookie; the explicit
        # cookie set on the test client is what the handler reads.
        auth_as(DEFAULT_TEST_USER_ID)
        client.cookies.set("criticalbit_access", "the-token")
        response = await client.get("/v1/tournaments/cup/owners")
        assert response.status_code == 200
        assert captured["token"] == "the-token"

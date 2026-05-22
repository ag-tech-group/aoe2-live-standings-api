"""Authentication and authorization for the write/management API.

Exercises the auth layer end to end through a representative write
endpoint (``PATCH /v1/tournaments/{slug}``): real RS256 cookies verified
against the stubbed JWKS, plus the ``tournament_owners`` access check.
Per-endpoint behaviour is covered in the router test modules.
"""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from tests.conftest import DEFAULT_TEST_USER_ID, make_access_token, make_tournament


def _cookie(token: str) -> dict[str, str]:
    """Build the Cookie header carrying an access token.

    Sent as an explicit header rather than httpx's per-request ``cookies=``
    argument, which is deprecated.
    """
    return {"Cookie": f"criticalbit_access={token}"}


class TestEveryWriteRouteIsGated:
    """Every write route must reject an unauthenticated request — a guard
    against an endpoint shipping without the auth dependency wired on."""

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("PATCH", "/v1/tournaments/cup", {}),
            ("POST", "/v1/tournaments/cup/players", {"profile_id": 1}),
            ("DELETE", "/v1/tournaments/cup/players/1", {}),
            ("POST", "/v1/tournaments/cup/teams", {"name": "Red", "initials": "RED"}),
            ("PATCH", "/v1/tournaments/cup/teams/1", {}),
            ("DELETE", "/v1/tournaments/cup/teams/1", {}),
            ("POST", "/v1/tournaments/cup/teams/1/members", {"profile_id": 1}),
            ("DELETE", "/v1/tournaments/cup/teams/1/members/1", {}),
        ],
    )
    async def test_unauthenticated_request_is_401(
        self, client: AsyncClient, method: str, path: str, body: dict
    ):
        response = await client.request(method, path, json=body)
        assert response.status_code == 401


class TestAuthentication:
    """Verifying the criticalbit_access cookie's JWT in get_current_user_id."""

    async def test_valid_token_for_owner_succeeds(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()
        token = make_access_token(DEFAULT_TEST_USER_ID)

        response = await client.patch(
            "/v1/tournaments/cup",
            json={"name": "Renamed"},
            headers=_cookie(token),
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Renamed"

    async def test_malformed_token_is_401(self, client: AsyncClient):
        response = await client.patch(
            "/v1/tournaments/cup",
            json={"name": "X"},
            headers=_cookie("this-is-not-a-jwt"),
        )
        assert response.status_code == 401

    async def test_expired_token_is_401(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()
        token = make_access_token(
            DEFAULT_TEST_USER_ID, exp=datetime.now(tz=UTC) - timedelta(minutes=1)
        )

        response = await client.patch(
            "/v1/tournaments/cup", json={"name": "X"}, headers=_cookie(token)
        )
        assert response.status_code == 401

    async def test_token_signed_by_unknown_key_is_401(
        self, client: AsyncClient, session: AsyncSession
    ):
        # A token whose signature can't be verified against the JWKS key —
        # this drives the refresh-and-retry branch, which still rejects it.
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()
        foreign_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = jwt.encode(
            {
                "sub": DEFAULT_TEST_USER_ID,
                "aud": ["fastapi-users:auth"],
                "exp": datetime.now(tz=UTC) + timedelta(minutes=15),
            },
            foreign_key,
            algorithm="RS256",
        )

        response = await client.patch(
            "/v1/tournaments/cup", json={"name": "X"}, headers=_cookie(token)
        )
        assert response.status_code == 401

    async def test_wrong_audience_is_401(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()
        token = make_access_token(DEFAULT_TEST_USER_ID, aud="urn:some-other-service")

        response = await client.patch(
            "/v1/tournaments/cup", json={"name": "X"}, headers=_cookie(token)
        )
        assert response.status_code == 401

    async def test_token_without_subject_is_401(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()
        token = make_access_token(sub=None)

        response = await client.patch(
            "/v1/tournaments/cup", json={"name": "X"}, headers=_cookie(token)
        )
        assert response.status_code == 401

    async def test_wrong_issuer_is_401_when_issuer_configured(
        self, client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(settings, "auth_token_issuer", "https://auth-api.criticalbit.gg")
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()
        token = make_access_token(DEFAULT_TEST_USER_ID, iss="https://impostor.example")

        response = await client.patch(
            "/v1/tournaments/cup", json={"name": "X"}, headers=_cookie(token)
        )
        assert response.status_code == 401

    async def test_matching_issuer_succeeds_when_issuer_configured(
        self, client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ):
        issuer = "https://auth-api.criticalbit.gg"
        monkeypatch.setattr(settings, "auth_token_issuer", issuer)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()
        token = make_access_token(DEFAULT_TEST_USER_ID, iss=issuer)

        response = await client.patch(
            "/v1/tournaments/cup", json={"name": "X"}, headers=_cookie(token)
        )
        assert response.status_code == 200


class TestAuthorization:
    """The tournament_owners access check in require_tournament_owner."""

    async def test_authenticated_non_owner_is_403(self, client: AsyncClient, session: AsyncSession):
        # The tournament exists, but the caller has no owner row for it.
        session.add(make_tournament("cup"))
        await session.commit()
        token = make_access_token(DEFAULT_TEST_USER_ID)

        response = await client.patch(
            "/v1/tournaments/cup", json={"name": "X"}, headers=_cookie(token)
        )
        assert response.status_code == 403

    async def test_owner_of_another_tournament_is_403(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Ownership is per-tournament: owning `other` grants nothing on `cup`.
        session.add(make_tournament("cup"))
        session.add(make_tournament("other", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()
        token = make_access_token(DEFAULT_TEST_USER_ID)

        response = await client.patch(
            "/v1/tournaments/cup", json={"name": "X"}, headers=_cookie(token)
        )
        assert response.status_code == 403

    async def test_unknown_tournament_is_404(self, client: AsyncClient):
        token = make_access_token(DEFAULT_TEST_USER_ID)

        response = await client.patch(
            "/v1/tournaments/nope", json={"name": "X"}, headers=_cookie(token)
        )
        assert response.status_code == 404

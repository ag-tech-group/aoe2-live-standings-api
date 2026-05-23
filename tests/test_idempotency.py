"""Idempotency-Key middleware tests (#61)."""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IdempotencyKey
from tests.conftest import DEFAULT_TEST_USER_ID


class TestIdempotencyKey:
    """End-to-end behavior of the middleware against POST /v1/tournaments."""

    def _body(self, slug: str = "spring-cup") -> dict:
        return {"slug": slug, "name": "Spring Cup", "leaderboard_id": 3}

    async def test_no_header_passes_through(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # Without an Idempotency-Key, the middleware is a no-op.
        auth_as(DEFAULT_TEST_USER_ID)
        r = await client.post("/v1/tournaments", json=self._body())
        assert r.status_code == 201
        assert (await session.execute(_count_keys())).scalar_one() == 0

    async def test_same_key_replays_cached_response(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # Two identical requests with the same key — second one must
        # come back from the cache; only one tournament should exist.
        auth_as(DEFAULT_TEST_USER_ID)
        key = str(uuid.uuid4())
        headers = {"Idempotency-Key": key}

        r1 = await client.post("/v1/tournaments", json=self._body(), headers=headers)
        assert r1.status_code == 201

        r2 = await client.post("/v1/tournaments", json=self._body(), headers=headers)
        assert r2.status_code == 201
        # Same body — proves the cache served the original response.
        assert r1.json() == r2.json()

        # Only one tournament was created despite two POSTs.
        tournaments = (await client.get("/v1/tournaments")).json()
        assert len(tournaments) == 1
        assert tournaments[0]["slug"] == "spring-cup"

    async def test_same_key_different_body_returns_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        key = str(uuid.uuid4())
        headers = {"Idempotency-Key": key}

        r1 = await client.post(
            "/v1/tournaments", json=self._body(slug="spring-cup"), headers=headers
        )
        assert r1.status_code == 201

        # Same key, different body — must be rejected.
        r2 = await client.post(
            "/v1/tournaments", json=self._body(slug="summer-cup"), headers=headers
        )
        assert r2.status_code == 422
        assert r2.json()["error_code"] == "idempotency_key_reused"

    async def test_invalid_key_format_returns_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        r = await client.post(
            "/v1/tournaments",
            json=self._body(),
            headers={"Idempotency-Key": "not-a-uuid"},
        )
        assert r.status_code == 422
        assert r.json()["error_code"] == "idempotency_key_invalid"

    async def test_4xx_response_is_cached(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # A 409 conflict counts as "the caller has their answer" — a
        # retry with the same key should replay it, not try again.
        from tests.conftest import make_tournament

        session.add(make_tournament("spring-cup"))
        await session.commit()
        auth_as(DEFAULT_TEST_USER_ID)
        key = str(uuid.uuid4())
        headers = {"Idempotency-Key": key}

        r1 = await client.post("/v1/tournaments", json=self._body(), headers=headers)
        assert r1.status_code == 409

        r2 = await client.post("/v1/tournaments", json=self._body(), headers=headers)
        assert r2.status_code == 409
        assert r1.json() == r2.json()

    async def test_read_endpoints_ignore_key(self, client: AsyncClient):
        # The middleware is scoped to write methods — a GET with an
        # Idempotency-Key header is just a normal GET.
        r = await client.get(
            "/v1/tournaments",
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 200


def _count_keys():
    from sqlalchemy import func, select

    return select(func.count()).select_from(IdempotencyKey)

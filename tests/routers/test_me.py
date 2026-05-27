"""GET /v1/me — identity + owned-tournament list."""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import DEFAULT_TEST_USER_ID, make_tournament

OTHER_USER_ID = "00000000-0000-0000-0000-0000000000bb"


class TestGetMe:
    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        r = await client.get("/v1/me")
        assert r.status_code == 401

    async def test_returns_user_id_with_empty_ownerships(self, client: AsyncClient, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        r = await client.get("/v1/me")
        assert r.status_code == 200
        body = r.json()
        assert body["user_id"] == DEFAULT_TEST_USER_ID
        assert body["owned_tournaments"] == []

    async def test_lists_owned_tournaments_newest_first(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # Three tournaments, two owned by the caller (with distinct
        # created_at), one owned by someone else.
        session.add(
            make_tournament(
                "older",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                owner_ids=[DEFAULT_TEST_USER_ID],
            )
        )
        session.add(
            make_tournament(
                "newer",
                created_at=datetime(2026, 2, 1, tzinfo=UTC),
                owner_ids=[DEFAULT_TEST_USER_ID],
            )
        )
        session.add(make_tournament("not-mine", owner_ids=[OTHER_USER_ID]))
        await session.commit()

        auth_as(DEFAULT_TEST_USER_ID)
        body = (await client.get("/v1/me")).json()
        slugs = [t["slug"] for t in body["owned_tournaments"]]
        # Owned-only, newest first; the not-mine tournament must be absent.
        assert slugs == ["newer", "older"]

    async def test_excludes_tournaments_user_does_not_own(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # If a tournament has multiple owners, only the owner querying
        # /v1/me sees it (and only because they're on the owners list,
        # not because of any other tournament metadata).
        session.add(make_tournament("shared", owner_ids=[DEFAULT_TEST_USER_ID, OTHER_USER_ID]))
        session.add(make_tournament("other-only", owner_ids=[OTHER_USER_ID]))
        await session.commit()

        auth_as(DEFAULT_TEST_USER_ID)
        body = (await client.get("/v1/me")).json()
        assert [t["slug"] for t in body["owned_tournaments"]] == ["shared"]

    async def test_response_carries_full_tournament_read(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # The response shape matches the standalone TournamentRead so
        # the frontend can render an "admin dashboard" without a second
        # fetch per slug.
        session.add(
            make_tournament(
                "cup",
                name="The Cup",
                leaderboard_id=3,
                start_date=datetime(2026, 6, 1, tzinfo=UTC),
                owner_ids=[DEFAULT_TEST_USER_ID],
            )
        )
        await session.commit()

        auth_as(DEFAULT_TEST_USER_ID)
        body = (await client.get("/v1/me")).json()
        cup = body["owned_tournaments"][0]
        assert cup["slug"] == "cup"
        assert cup["name"] == "The Cup"
        assert cup["leaderboard_id"] == 3
        assert cup["start_date"].startswith("2026-06-01")
        # grand_finals_date is part of TournamentRead (post-#44).
        assert "grand_finals_date" in cup

    async def test_cache_control_is_private_no_store(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # /v1/me is per-user content. The global cache middleware would
        # otherwise stamp `public, max-age=3600`, which (a) caches stale
        # admin-grant state across mutations and (b) is structurally
        # unsafe for shared caches like Cloudflare to serve one user's
        # response to another. The handler sets an explicit override.
        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.get("/v1/me")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "private, no-store"

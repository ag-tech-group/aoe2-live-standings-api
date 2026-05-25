"""Unit tests for the criticalbit-auth-api users client.

The conftest's autouse ``stub_users_client`` fixture patches the router's
binding of ``fetch_identities`` to a no-op but doesn't touch
``app.auth.users_client.fetch_identities`` itself — these tests call the
real function and mock the underlying HTTP layer with respx.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.auth import users_client
from app.config import settings

# Two valid-shaped UUIDs to use as test ids.
UID_A = "11111111-1111-1111-1111-111111111111"
UID_B = "22222222-2222-2222-2222-222222222222"
UID_MISSING = "33333333-3333-3333-3333-333333333333"


def _identity_row(uid: str, **overrides) -> dict:
    """Build a fake /users/lookup row with the auth-api response shape."""
    return {
        "id": uid,
        "email": f"{uid[:8]}@example.com",
        "display_name": f"name-{uid[:4]}",
        "avatar_url": None,
        **overrides,
    }


class TestFetchIdentities:
    """``fetch_identities`` — cache, batching, degradation, cookie passthrough."""

    async def test_empty_input_returns_empty_without_calling_auth_api(self):
        # No respx mock; if the function tried to make a request it would error.
        result = await users_client.fetch_identities([], access_token="tok")
        assert result == {}

    async def test_returns_parsed_identities(self):
        with respx.mock(base_url=settings.auth_api_base_url) as mock:
            mock.get("/users/lookup").mock(
                return_value=httpx.Response(
                    200,
                    json=[
                        {
                            "id": UID_A,
                            "email": "hera@example.com",
                            "display_name": "Hera",
                            "avatar_url": "https://example.com/hera.png",
                        }
                    ],
                )
            )
            result = await users_client.fetch_identities([UID_A], access_token="tok")

        assert result == {
            UID_A: users_client.UserIdentity(
                user_id=UID_A,
                email="hera@example.com",
                display_name="Hera",
                avatar_url="https://example.com/hera.png",
            )
        }

    async def test_forwards_access_token_as_cookie(self):
        with respx.mock(base_url=settings.auth_api_base_url) as mock:
            route = mock.get("/users/lookup").mock(
                return_value=httpx.Response(200, json=[_identity_row(UID_A)])
            )
            await users_client.fetch_identities([UID_A], access_token="my-token")

        assert route.calls[0].request.headers.get("cookie") == "criticalbit_access=my-token"

    async def test_no_token_sends_no_cookie(self):
        # No token + 401 from upstream is the documented degraded path — we still
        # make the call (so the caller sees the structured failure log) but the
        # function swallows the resulting error and returns an empty dict.
        with respx.mock(base_url=settings.auth_api_base_url) as mock:
            route = mock.get("/users/lookup").mock(return_value=httpx.Response(401))
            result = await users_client.fetch_identities([UID_A], access_token=None)

        assert result == {}
        assert "cookie" not in route.calls[0].request.headers

    async def test_dedupes_repeated_ids_in_request(self):
        with respx.mock(base_url=settings.auth_api_base_url) as mock:
            route = mock.get("/users/lookup").mock(
                return_value=httpx.Response(200, json=[_identity_row(UID_A)])
            )
            await users_client.fetch_identities([UID_A, UID_A, UID_A], access_token="tok")

        # Single request; the id appears once in the outbound query.
        assert route.calls[0].request.url.params.get_list("ids") == [UID_A]

    async def test_caches_identity_for_subsequent_calls(self):
        with respx.mock(base_url=settings.auth_api_base_url) as mock:
            route = mock.get("/users/lookup").mock(
                return_value=httpx.Response(200, json=[_identity_row(UID_A)])
            )
            await users_client.fetch_identities([UID_A], access_token="tok")
            await users_client.fetch_identities([UID_A], access_token="tok")
            await users_client.fetch_identities([UID_A], access_token="tok")

        # One request despite three calls — second + third served from cache.
        assert route.call_count == 1

    async def test_only_misses_are_fetched_on_mixed_call(self):
        # Prime cache with UID_A.
        with respx.mock(base_url=settings.auth_api_base_url) as mock:
            mock.get("/users/lookup").mock(
                return_value=httpx.Response(200, json=[_identity_row(UID_A)])
            )
            await users_client.fetch_identities([UID_A], access_token="tok")

        # Now ask for both; only UID_B should hit the wire.
        with respx.mock(base_url=settings.auth_api_base_url) as mock:
            route = mock.get("/users/lookup").mock(
                return_value=httpx.Response(200, json=[_identity_row(UID_B)])
            )
            result = await users_client.fetch_identities([UID_A, UID_B], access_token="tok")

        assert route.calls[0].request.url.params.get_list("ids") == [UID_B]
        assert {UID_A, UID_B} <= set(result.keys())

    async def test_unknown_ids_are_omitted_from_result(self):
        # auth-api silently drops unknown ids from the response.
        with respx.mock(base_url=settings.auth_api_base_url) as mock:
            mock.get("/users/lookup").mock(
                return_value=httpx.Response(200, json=[_identity_row(UID_A)])
            )
            result = await users_client.fetch_identities([UID_A, UID_MISSING], access_token="tok")

        assert UID_A in result
        assert UID_MISSING not in result

    async def test_5xx_returns_cached_subset_without_raising(self):
        # Prime cache with UID_A.
        with respx.mock(base_url=settings.auth_api_base_url) as mock:
            mock.get("/users/lookup").mock(
                return_value=httpx.Response(200, json=[_identity_row(UID_A)])
            )
            await users_client.fetch_identities([UID_A], access_token="tok")

        # Now auth-api blows up on UID_B; the call should swallow + log,
        # returning the cached UID_A and omitting UID_B (rather than raising
        # or returning nothing).
        with respx.mock(base_url=settings.auth_api_base_url) as mock:
            mock.get("/users/lookup").mock(return_value=httpx.Response(500))
            result = await users_client.fetch_identities([UID_A, UID_B], access_token="tok")

        assert UID_A in result
        assert UID_B not in result

    async def test_network_error_returns_cached_subset_without_raising(self):
        # Prime cache with UID_A.
        with respx.mock(base_url=settings.auth_api_base_url) as mock:
            mock.get("/users/lookup").mock(
                return_value=httpx.Response(200, json=[_identity_row(UID_A)])
            )
            await users_client.fetch_identities([UID_A], access_token="tok")

        # Now the next call raises mid-flight.
        with respx.mock(base_url=settings.auth_api_base_url) as mock:
            mock.get("/users/lookup").mock(side_effect=httpx.ConnectError("boom"))
            result = await users_client.fetch_identities([UID_A, UID_B], access_token="tok")

        assert UID_A in result
        assert UID_B not in result

    async def test_nulls_preserved_in_parsed_identity(self):
        with respx.mock(base_url=settings.auth_api_base_url) as mock:
            mock.get("/users/lookup").mock(
                return_value=httpx.Response(
                    200,
                    json=[
                        {
                            "id": UID_A,
                            "email": "x@y",
                            "display_name": None,
                            "avatar_url": None,
                        }
                    ],
                )
            )
            result = await users_client.fetch_identities([UID_A], access_token="tok")

        assert result[UID_A].display_name is None
        assert result[UID_A].avatar_url is None
        assert result[UID_A].email == "x@y"

    async def test_ttl_expiry_re_fetches(self, monkeypatch: pytest.MonkeyPatch):
        # Drive time forward past the TTL between two calls by stubbing
        # the module's `time.monotonic`. Avoids real sleeps.
        clock = {"t": 1000.0}

        def _now():
            return clock["t"]

        monkeypatch.setattr(users_client.time, "monotonic", _now)

        with respx.mock(base_url=settings.auth_api_base_url) as mock:
            route = mock.get("/users/lookup").mock(
                return_value=httpx.Response(200, json=[_identity_row(UID_A)])
            )
            await users_client.fetch_identities([UID_A], access_token="tok")

            # Advance past the TTL window — the next call must re-fetch.
            clock["t"] += users_client._CACHE_TTL_SECONDS + 1
            await users_client.fetch_identities([UID_A], access_token="tok")

        assert route.call_count == 2

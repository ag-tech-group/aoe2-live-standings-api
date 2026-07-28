"""App-level / infrastructure route behaviour: security.txt and rate-limit exemptions."""

from httpx import AsyncClient

# Any origin the credentialed CORS config does NOT allow — the third-party
# frontend case the open-reads posture (#297) exists for. Dev config allows
# localhost:5100-5199, so this never collides.
_THIRD_PARTY_ORIGIN = "https://thirdparty.example"
# An origin the credentialed first-party posture allows in dev/tests.
_FIRST_PARTY_ORIGIN = "http://localhost:5100"


class TestPublicReadCORS:
    """Open cross-origin reads (#297): `*` for third parties, credentialed
    echo for first-party origins, and nothing on writes or /v1/me."""

    async def test_third_party_read_gets_wildcard(self, client: AsyncClient):
        response = await client.get("/v1/flags", headers={"Origin": _THIRD_PARTY_ORIGIN})
        assert response.status_code == 200
        assert response.headers["Access-Control-Allow-Origin"] == "*"
        # Never credentials with `*` — the spec forbids the combination and
        # the layer must not invite cookie-bearing cross-site requests.
        assert "Access-Control-Allow-Credentials" not in response.headers

    async def test_first_party_origin_keeps_credentialed_echo(self, client: AsyncClient):
        # The inner CORSMiddleware already answered — the open layer must
        # defer, not double-set or downgrade to `*`.
        response = await client.get("/v1/flags", headers={"Origin": _FIRST_PARTY_ORIGIN})
        assert response.headers["Access-Control-Allow-Origin"] == _FIRST_PARTY_ORIGIN
        assert response.headers["Access-Control-Allow-Credentials"] == "true"

    async def test_me_is_never_opened(self, client: AsyncClient):
        # /v1/me is a GET but per-user + cookie-authenticated — the one /v1
        # read the open posture excludes.
        response = await client.get("/v1/me", headers={"Origin": _THIRD_PARTY_ORIGIN})
        assert response.status_code == 401
        assert "Access-Control-Allow-Origin" not in response.headers

    async def test_third_party_write_gets_nothing(self, client: AsyncClient):
        response = await client.post(
            "/v1/tournaments",
            headers={"Origin": _THIRD_PARTY_ORIGIN},
            json={"slug": "x", "name": "X", "leaderboard_id": 3},
        )
        assert "Access-Control-Allow-Origin" not in response.headers

    async def test_no_origin_header_means_no_cors_headers(self, client: AsyncClient):
        response = await client.get("/v1/flags")
        assert "Access-Control-Allow-Origin" not in response.headers

    async def test_infra_routes_stay_same_origin(self, client: AsyncClient):
        # The open posture covers /v1 only — infra routes are not part of
        # the consumable read surface.
        response = await client.get("/health", headers={"Origin": _THIRD_PARTY_ORIGIN})
        assert "Access-Control-Allow-Origin" not in response.headers


class TestCacheControlDefault:
    """The cache middleware defaults cacheless 200 GETs to `no-store` (#103).

    Caching is opt-in: endpoints that benefit set their own header. A route
    that stays silent — like the health/liveness probes — must NOT be
    publicly cached (the old `public, max-age=3600` default was the root
    cause of the #101/#104/#105 staleness + cross-user-cache bugs).
    """

    async def test_health_probe_is_not_cached(self, client: AsyncClient):
        # /health sets no Cache-Control of its own → falls to the default.
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"

    async def test_root_is_not_cached(self, client: AsyncClient):
        response = await client.get("/")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"


class TestSecurityTxt:
    async def test_served_as_plain_text_with_required_fields(self, client: AsyncClient):
        response = await client.get("/.well-known/security.txt")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        body = response.text
        assert "Contact:" in body
        assert "Expires:" in body
        # Canonical field per RFC 9116 — pins this URL so the file can't be
        # claimed by a third party serving a copy elsewhere.
        assert "Canonical:" in body
        # Regression guard: the file shipped with `security@example.com`
        # as a placeholder and was served live for a stretch before being
        # caught. Don't let that happen again.
        assert "example.com" not in body


class TestGlobalRateLimit:
    """The Limiter's ``default_limits`` (300/min, enforced by
    SlowAPIMiddleware) applies to every route without its own
    ``@limiter.limit`` — the canary for the class of regression where a
    framework upgrade silently disables middleware enforcement (slowapi
    issue #281 for fastapi>=0.137)."""

    async def test_default_limit_enforced_on_undecorated_route(self, client: AsyncClient):
        # GET /v1/tournaments has no explicit @limiter.limit, so it gets the
        # 300/min default. (Matches `default_limits` in app/limiting.py —
        # bump both together if that changes.)
        for _ in range(300):
            assert (await client.get("/v1/tournaments")).status_code == 200
        assert (await client.get("/v1/tournaments")).status_code == 429


class TestRateLimitExemptions:
    """/health, /, /docs, /.well-known/security.txt are @limiter.exempt — many
    rapid hits (well past the 300/min default) never 429."""

    async def test_infrastructure_routes_are_exempt(self, client: AsyncClient):
        for _ in range(310):
            assert (await client.get("/health")).status_code == 200
        assert (await client.get("/")).status_code == 200
        assert (await client.get("/.well-known/security.txt")).status_code == 200

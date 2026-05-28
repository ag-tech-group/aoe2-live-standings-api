"""App-level / infrastructure route behaviour: security.txt and rate-limit exemptions."""

from httpx import AsyncClient


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


class TestRateLimitExemptions:
    """/health, /, /docs, /.well-known/security.txt are @limiter.exempt — many
    rapid hits (well past the 60/min default) never 429."""

    async def test_infrastructure_routes_are_exempt(self, client: AsyncClient):
        for _ in range(70):
            assert (await client.get("/health")).status_code == 200
        assert (await client.get("/")).status_code == 200
        assert (await client.get("/.well-known/security.txt")).status_code == 200

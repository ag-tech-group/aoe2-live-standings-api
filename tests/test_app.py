"""App-level / infrastructure route behaviour: security.txt and rate-limit exemptions."""

from httpx import AsyncClient


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

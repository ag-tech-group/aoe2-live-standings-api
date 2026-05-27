"""Cache-Control helpers for live read endpoints.

The endpoints in this module's call sites are read by two distinct
audiences with conflicting needs:

- **Unauthenticated viewers** want CDN coalescing. At event-window
  scale (see ``docs/event-traffic-cost-model.md``) the same response
  is read thousands of times per second; ``s-maxage`` lets Cloudflare
  hold one copy and serve all of them, dropping origin load by
  orders of magnitude.
- **Authenticated admins** want immediate read-after-write freshness.
  An admin who just PATCHed a tournament, added a player to a team,
  or revoked an owner expects the next read to reflect the new
  state. Any CDN-held copy makes that flow fail intermittently —
  the failure is timing-dependent on whether a recent viewer load
  refreshed the edge cache.

``apply_live_cache_control`` branches on the access-cookie's presence
to give each audience the right header. See its docstring for the
full contract, and the file-level "Important" note below.

------------------------------------------------------------------
**Important: origin headers alone do not fully prevent CF from
serving cached responses to admins.** CF's cache lookup happens
before origin is consulted. If a viewer's request populated the cache,
CF will serve that cached response to a subsequent admin request —
the admin's cookie is invisible to the cache key, and the origin's
``private, no-store`` header is never consulted.

The complete fix needs a matching Cloudflare Cache Rule that bypasses
cache when the ``criticalbit_access`` cookie is present:

    Match:  Hostname equals aoe2-live-standings-api.criticalbit.gg
        AND Cookie value matches regex `.+` (for cookie name `criticalbit_access`)
    Then:   Cache eligibility = Bypass cache

This rule should be ordered *above* the existing "use cache-control
header from origin" rule so authenticated requests skip the cache
lookup entirely. See ``infra/README.md`` for the dashboard recipe.

Without the CF rule, the headers this helper sets are still correct
HTTP and protect against other proxies / future CF reconfigurations,
but the user-visible staleness symptom in #105 will persist.
------------------------------------------------------------------
"""

from __future__ import annotations

from fastapi import Request, Response

from app.auth.dependencies import ACCESS_TOKEN_COOKIE


def apply_live_cache_control(request: Request, response: Response, *, cdn_seconds: int) -> None:
    """Stamp Cache-Control on a live read endpoint, branching on auth state.

    - Authenticated callers (the access-token cookie is present):
      ``private, no-store``. Skips every cache layer for this request;
      admin reads always reach origin.
    - Unauthenticated callers (no access cookie):
      ``public, s-maxage=<cdn_seconds>, max-age=0, must-revalidate``.
      CF holds the response for ``cdn_seconds``; the browser always
      revalidates. Same pattern PR #99 shipped for #96.

    ``Vary: Cookie`` is set on every response. CF does not natively
    respect it (would tank hit rates on the wider web), but the header
    is correct HTTP and protects any other proxy in the path.

    The branch's purely "is the cookie present?" — we do not verify the
    JWT here. A request with a malformed or expired token still gets
    the auth-side header, which is harmless (still skips cache; the
    actual auth dependency, if any, will reject the request). The
    point is to keep the cache-control decision cheap and consistent
    with what the proxy can reasonably know.
    """
    has_auth = bool(request.cookies.get(ACCESS_TOKEN_COOKIE))
    if has_auth:
        response.headers["Cache-Control"] = "private, no-store"
    else:
        response.headers["Cache-Control"] = (
            f"public, s-maxage={cdn_seconds}, max-age=0, must-revalidate"
        )
    response.headers["Vary"] = "Cookie"

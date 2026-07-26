"""Open cross-origin READ access for third-party frontends (#297).

The credentialed ``CORSMiddleware`` in ``app.main`` serves the first-party
surface: criticalbit origins get their origin echoed with
``Access-Control-Allow-Credentials`` so the management cookie flows. That
posture can't also grant arbitrary origins read access — credentialed CORS
forbids ``*``, and reflecting arbitrary origins with credentials would let
any site ride a logged-in user's cookie.

This layer adds the second posture: plain ``GET``/``HEAD`` requests to the
public ``/v1`` read surface from any *other* origin get a literal
``Access-Control-Allow-Origin: *`` (no credentials). Design constraints,
from the #297 notes:

- **Literal ``*``, never an echo** — an echoed origin requires
  ``Vary: Origin``, which fragments CDN cache entries per origin; ``*`` is
  origin-independent and cache-safe. This layer never touches ``Vary``.
- **Never two ``Access-Control-Allow-Origin`` headers** — if the inner
  CORSMiddleware already answered (an allowed first-party origin), this
  layer stays out of it.
- **``/v1/me`` is excluded** — a GET, but per-user and cookie-authenticated;
  it stays first-party-only.
- **Simple requests only** — plain GETs (and ``EventSource`` for
  ``/v1/stream``) never preflight, so no ``OPTIONS`` handling is needed.
  A third-party ``fetch`` that adds custom headers would preflight and is
  deliberately unsupported; the read surface needs none.

Pure ASGI (no ``BaseHTTPMiddleware``) so the streaming ``/v1/stream``
response passes through untouched.
"""

from __future__ import annotations

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import API_V1_PREFIX

# The one /v1 read that must never be opened: per-user, cookie-authenticated.
_PRIVATE_READ_PREFIX = f"{API_V1_PREFIX}/me"


class PublicReadCORSMiddleware:
    """Grant ``Access-Control-Allow-Origin: *`` on public /v1 reads.

    Must be registered OUTSIDE the credentialed ``CORSMiddleware`` (added
    after it), so it sees — and defers to — any allow-origin header the
    first-party posture already set.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        eligible = (
            scope.get("method") in ("GET", "HEAD")
            and Headers(scope=scope).get("origin") is not None
            and path.startswith(f"{API_V1_PREFIX}/")
            and path != _PRIVATE_READ_PREFIX
            and not path.startswith(f"{_PRIVATE_READ_PREFIX}/")
        )
        if not eligible:
            await self.app(scope, receive, send)
            return

        async def send_with_open_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "access-control-allow-origin" not in headers:
                    headers["Access-Control-Allow-Origin"] = "*"
                    # The credentialed CORSMiddleware stamps
                    # `allow-credentials: true` on every response, allowed
                    # origin or not. Paired with `*` that's a spec-invalid
                    # combination — and this branch only runs when the origin
                    # was NOT first-party, so the header is a stray here, not
                    # a grant being revoked. Strip it.
                    del headers["access-control-allow-credentials"]
            await send(message)

        await self.app(scope, receive, send_with_open_cors)

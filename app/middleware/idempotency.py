"""Idempotency-Key middleware (#61).

When a write request carries an `Idempotency-Key` header, the
middleware looks the key up in `idempotency_keys`:

  - **Hit, same fingerprint** → return the cached response. The
    handler does not re-execute, so the side effect is not repeated.
  - **Hit, different fingerprint** → 422 `idempotency_key_reused`,
    signalling that the client reused a key for a genuinely
    different request (likely a bug).
  - **Miss** → the request proceeds normally; after the handler
    returns a 2xx/4xx response, the response is cached under the
    key for 24h.

Behavior is opt-in via the `Idempotency-Key` header — no header
means today's behavior (no caching). 5xx responses are not cached,
since the failure is presumed transient.

Scoped to write methods (POST/PATCH/DELETE) on /v1/* — read paths
don't need idempotency and the auth/health/docs endpoints would only
add overhead.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app import database
from app.models import IdempotencyKey

# Only intercept these methods. Reads are inherently idempotent;
# OPTIONS preflight needs to pass through unmodified.
_WRITE_METHODS = frozenset({"POST", "PATCH", "DELETE", "PUT"})

# Only apply to versioned API routes. Catches /v1/tournaments/...
# but not /docs, /health, /.well-known/* etc.
_PATH_PATTERN = re.compile(r"^/v\d+/")

# UUID format — same regex the tournament-owner schema uses (#37).
# Accept anything UUID-shaped; reject malformed keys with 422.
_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _fingerprint(method: str, path: str, body: bytes) -> str:
    """SHA-256 of method + path + body. Same request → same fingerprint."""
    h = hashlib.sha256()
    h.update(method.encode())
    h.update(b" ")
    h.update(path.encode())
    h.update(b" ")
    h.update(body)
    return h.hexdigest()


def _envelope_response(
    request: Request,
    *,
    status_code: int,
    error_code: str,
    message: str,
    details: Any = None,
) -> JSONResponse:
    """Build a `BusinessError`-shaped envelope response inline.

    Starlette's `BaseHTTPMiddleware` doesn't propagate exceptions to
    FastAPI's exception handlers cleanly, so the middleware returns
    its error responses directly instead of raising. Shape matches
    `app/errors.py`'s `BusinessError` handler exactly.
    """
    return JSONResponse(
        status_code=status_code,
        content={
            "error_code": error_code,
            "message": message,
            "request_id": getattr(request.state, "request_id", "unknown"),
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "details": details,
        },
    )


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that caches write responses keyed by header."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method not in _WRITE_METHODS or not _PATH_PATTERN.match(request.url.path):
            return await call_next(request)

        key = request.headers.get("Idempotency-Key")
        if not key:
            return await call_next(request)

        if not _UUID_PATTERN.match(key):
            # Malformed key — surface as a structured error rather
            # than silently treating the request as un-keyed.
            return _envelope_response(
                request,
                status_code=422,
                error_code="idempotency_key_invalid",
                message="Idempotency-Key must be a UUID",
            )

        # Buffer the body so we can fingerprint it AND replay it to
        # the downstream handler. Starlette's Request.body() reads
        # the underlying stream once; after we read it here, we
        # rebuild the receive callable so the handler sees the same
        # bytes.
        body = await request.body()
        fingerprint = _fingerprint(request.method, request.url.path, body)

        # Look up the session maker on `app.database` each time so the
        # test suite can monkey-patch `app.database.async_session_maker`
        # to its test session (SQLite + the same schema). Importing the
        # symbol at module load would freeze the production maker here.
        async with database.async_session_maker() as session:
            existing = await session.get(IdempotencyKey, key)

            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    return _envelope_response(
                        request,
                        status_code=422,
                        error_code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was used previously with a different "
                            "request body or path"
                        ),
                    )
                return Response(
                    content=existing.response_body,
                    status_code=existing.response_status,
                    media_type="application/json",
                )

            # Cache miss — replay the buffered body to the downstream
            # handler. Starlette consumes the stream once, so we have
            # to inject a synthetic `receive` that re-emits the bytes.
            request._receive = _make_replay_receive(body)
            response = await call_next(request)

            # Only cache 2xx/4xx — 5xx is presumed transient; caching
            # it would lock in a failure and make retries pointless.
            if 200 <= response.status_code < 500:
                response_body = await _read_response_body(response)
                session.add(
                    IdempotencyKey(
                        key=key,
                        request_fingerprint=fingerprint,
                        response_status=response.status_code,
                        response_body=response_body,
                    )
                )
                await session.commit()
                # The original response's body stream has been drained
                # by `_read_response_body`; rebuild it for the client.
                return Response(
                    content=response_body,
                    status_code=response.status_code,
                    media_type=response.media_type,
                    headers={
                        k: v
                        for k, v in response.headers.items()
                        # Skip Content-Length — Response recomputes it.
                        if k.lower() not in {"content-length"}
                    },
                )
            return response


def _make_replay_receive(body: bytes) -> Callable[[], Awaitable[dict[str, Any]]]:
    """Build a synthetic ASGI `receive` that emits `body` once.

    Used after we drain `request.body()` so the downstream handler
    can read the same bytes without a "stream consumed" error.
    """
    state = {"sent": False}

    async def receive() -> dict[str, Any]:
        if state["sent"]:
            return {"type": "http.disconnect"}
        state["sent"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


async def _read_response_body(response: Response) -> bytes:
    """Drain a Starlette `Response`'s body to bytes.

    `JSONResponse` (and most other Response types we use) keep their
    body in `.body`; falling back to iterating the body stream covers
    streaming responses.
    """
    if hasattr(response, "body") and isinstance(response.body, bytes | bytearray):
        return bytes(response.body)
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:  # type: ignore[attr-defined]
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    return b"".join(chunks)


# Re-export JSON helper that callers occasionally need.
__all__ = ["IdempotencyMiddleware", "json"]

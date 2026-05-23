"""Error response envelope + handlers.

Today's error shapes vary by source: FastAPI's `HTTPException` produces
`{"detail": "..."}`, pydantic produces `{"detail": [{...}]}`, the rate-
limit handler produces its own shape. This module introduces a single
envelope that any /v1/* error can use:

    {
      "error_code": "tournament_not_found",  # stable, machine-readable
      "message": "Tournament not found",     # human-readable, EN
      "request_id": "abc-…",                 # matches X-Request-ID
      "timestamp": "2026-05-23T…Z",          # ISO-8601 UTC
      "details": null                        # validation payload, etc.
    }

Rollout: `BusinessError` (a new exception class) *always* uses the
envelope. `HTTPException` and `RequestValidationError` use the
envelope only when the `FEATURE_ERROR_ENVELOPE_V2` flag is on. That
lets new handlers raise `BusinessError` from day one while legacy
`raise HTTPException(...)` call sites keep returning the old shape
until the frontend coordinates on the new shape and the flag flips.

When the flag flips ON (or the existing call sites are migrated), the
old `{"detail": ...}` shape disappears entirely.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings


class BusinessError(Exception):
    """A domain-level error that always renders to the v1 envelope.

    Use this for new error cases instead of `HTTPException` so the
    response shape doesn't depend on the feature flag. Existing
    `HTTPException` call sites can migrate over incrementally.
    """

    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        details: Any = None,
    ) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.details = details
        super().__init__(message)


_STATUS_TO_ERROR_CODE: dict[int, str] = {
    400: "bad_request",
    401: "not_authenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_server_error",
}


def _envelope(
    *,
    error_code: str,
    message: str,
    request_id: str,
    details: Any = None,
) -> dict[str, Any]:
    """Build the envelope payload. Timestamp is generated here, so a
    response always carries one even if the caller didn't pass it."""
    return {
        "error_code": error_code,
        "message": message,
        "request_id": request_id,
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "details": details,
    }


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


async def _business_error_handler(request: Request, exc: BusinessError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(
            error_code=exc.error_code,
            message=exc.message,
            request_id=_request_id(request),
            details=exc.details,
        ),
    )


async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Render an HTTPException either as the new envelope (when the
    flag is on) or as FastAPI's legacy `{"detail": ...}` shape."""
    if settings.feature_error_envelope_v2:
        # `detail` can be a string ("Not authenticated") or a structured
        # object (pydantic validation array). Push the structured form
        # into `details` and synthesize a generic message from the status.
        if isinstance(exc.detail, str):
            message = exc.detail
            details: Any = None
        else:
            message = _STATUS_TO_ERROR_CODE.get(exc.status_code, "error").replace("_", " ")
            details = exc.detail
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(
                error_code=_STATUS_TO_ERROR_CODE.get(exc.status_code, "error"),
                message=message,
                request_id=_request_id(request),
                details=details,
            ),
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        # Preserve any Retry-After / WWW-Authenticate headers the
        # original HTTPException set (slowapi sets Retry-After on 429s).
        headers=getattr(exc, "headers", None),
    )


async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """RequestValidationError renders either as the envelope (with
    pydantic errors in `details`) or as FastAPI's legacy shape.

    `jsonable_encoder` on `exc.errors()` matches FastAPI's stock handler
    — pydantic 2's error entries include Python objects (e.g. URLs,
    contexts) that `JSONResponse`'s default JSON encoder can't serialize.
    """
    errors = jsonable_encoder(exc.errors())
    if settings.feature_error_envelope_v2:
        return JSONResponse(
            status_code=422,
            content=_envelope(
                error_code="validation_error",
                message="Request validation failed",
                request_id=_request_id(request),
                details=errors,
            ),
        )
    return JSONResponse(status_code=422, content={"detail": errors})


def register_error_handlers(app: FastAPI) -> None:
    """Wire all three handlers onto the FastAPI app. Call after
    middleware registration so `request.state.request_id` is populated
    by the time these handlers run."""
    app.add_exception_handler(BusinessError, _business_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)

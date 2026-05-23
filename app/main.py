import time
import uuid

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from scalar_fastapi import get_scalar_api_reference
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.config import API_V1_PREFIX, settings
from app.errors import register_error_handlers
from app.features import router as features_router
from app.limiting import limiter
from app.logging import setup_logging
from app.middleware.idempotency import IdempotencyMiddleware
from app.poller.lifespan import lifespan
from app.routers import (
    leaderboards_router,
    live_router,
    matches_router,
    me_router,
    owners_router,
    players_router,
    stream_router,
    teams_router,
    tournaments_router,
)
from app.sentry import init_sentry
from app.telemetry import setup_telemetry

setup_logging()
# Sentry init must run *before* the FastAPI app is constructed — the
# SDK's middleware auto-instrumentation hooks the app at construction
# time. A late init silently misses the request middleware chain.
init_sentry()
logger = structlog.get_logger("app.request")

app = FastAPI(
    title="AoE2 Live Standings API",
    description="Open-source live-standings API for AoE2: DE tournaments.",
    version="0.0.1",
    docs_url=None,
    lifespan=lifespan,
)

setup_telemetry(app)

# `allow_credentials=True` so the browser sends the `criticalbit_access`
# cookie on write requests; paired with an explicit origin list + regex
# (never `*`, which credentialed CORS forbids). Reads stay usable without
# credentials — the setting is a superset of the old behaviour.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_origin_regex=settings.cors_origin_regex,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)

# `default_limits` on the limiter applies to every route that isn't
# decorated with its own `@limiter.limit(...)` or marked `@limiter.exempt`.
# `get_remote_address` reads `request.client.host`, which is the real
# client IP only when uvicorn runs with `--proxy-headers` behind a trusted
# proxy (see start.sh); without that, every request collapses into one
# bucket and the limit is useless. Per-endpoint limits live with each
# route in the router modules (see #60).
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

MAX_REQUEST_BODY_SIZE = 1_048_576


@app.middleware("http")
async def limit_request_body_size(request: Request, call_next) -> Response:
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_REQUEST_BODY_SIZE:
        return JSONResponse(
            status_code=413,
            content={"detail": "Request body too large"},
        )
    return await call_next(request)


@app.middleware("http")
async def add_security_headers(request: Request, call_next) -> Response:
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


@app.middleware("http")
async def request_id_middleware(request: Request, call_next) -> Response:
    """Assign a unique request ID to every request."""
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = request_id
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.middleware("http")
async def cache_control_middleware(request: Request, call_next) -> Response:
    """Stamp a default Cache-Control on successful GETs that didn't set one.

    Per-route handlers can override by setting ``response.headers["Cache-Control"]``
    themselves (e.g. ``max-age=10`` on the live feed, ``no-store`` while a match
    is in progress). This middleware only fills in the conservative 1-hour
    default when the route stayed silent.
    """
    response = await call_next(request)
    if (
        request.method == "GET"
        and response.status_code == 200
        and "cache-control" not in response.headers
    ):
        response.headers["Cache-Control"] = "public, max-age=3600"
    return response


_HEALTH_PATHS = frozenset(("/", "/health"))


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next) -> Response:
    """Log method, path, status code, and duration for every request."""
    start = time.perf_counter()
    response = await call_next(request)
    if request.url.path not in _HEALTH_PATHS:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.info(
            "request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
    return response


# Idempotency-Key middleware (#61). Buffers + caches write responses
# keyed by client-supplied UUID header. No-op when the header is
# absent. Scoped to /v1/* writes only.
app.add_middleware(IdempotencyMiddleware)


# Error handlers — wired after the request-ID middleware so
# `request.state.request_id` is populated by the time a handler runs.
# `BusinessError` always responds with the new envelope; `HTTPException`
# + validation errors respect the `FEATURE_ERROR_ENVELOPE_V2` flag.
register_error_handlers(app)


# Application routers, all mounted under /v1 so the whole API surface is
# versioned together. Add new resource routers to this tuple — they're loop-
# mounted with the /v1 prefix automatically.
ROUTERS = (
    features_router,
    me_router,
    players_router,
    leaderboards_router,
    tournaments_router,
    teams_router,
    owners_router,
    matches_router,
    live_router,
    stream_router,
)
for router in ROUTERS:
    app.include_router(router, prefix=API_V1_PREFIX)


# Infrastructure routes — not part of the versioned API, and exempt from the
# global rate limit so health-check probes, doc/spec fetches, and automated
# scanners can't burn through the quota.
@app.get("/docs", include_in_schema=False)
@limiter.exempt
async def scalar_docs():
    """Scalar API documentation."""
    return get_scalar_api_reference(
        openapi_url=app.openapi_url,
        title=app.title,
    )


@app.get("/")
@limiter.exempt
async def root():
    """Service info."""
    return {"status": "ok", "service": "aoe2-live-standings-api"}


@app.get("/health")
@limiter.exempt
async def health_check():
    """Liveness probe."""
    return {"status": "healthy"}


# Per https://securitytxt.org/ — security researchers and automated scanners
# look for this file to find a disclosure contact. Replace the contact before
# deploying and bump Expires before the date below.
SECURITY_TXT = """\
Contact: mailto:security@example.com
Expires: 2027-05-12T00:00:00.000Z
Preferred-Languages: en
"""


@app.get("/.well-known/security.txt", include_in_schema=False)
@limiter.exempt
async def security_txt() -> PlainTextResponse:
    return PlainTextResponse(SECURITY_TXT)

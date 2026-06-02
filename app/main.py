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
    fan_votes_router,
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
    description=(
        "Open-source live-standings API for AoE2: DE tournaments.\n\n"
        "---\n\n"
        "Age of Empires II © Microsoft Corporation. AoE2 Live Standings API "
        "was created under Microsoft's [Game Content Usage Rules]"
        "(https://www.xbox.com/en-us/developers/rules) using assets from "
        "Age of Empires II and it is not endorsed by or affiliated with "
        "Microsoft."
    ),
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
    # `Idempotency-Key` is a custom request header on every write
    # (#61); browsers fire a CORS preflight to confirm it's allowed
    # before sending the real request. Without it in this list, every
    # write from a browser fails at preflight.
    allow_headers=["Content-Type", "Idempotency-Key"],
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
    """Default cacheless GETs to ``no-store`` — caching is opt-in (#103).

    A handler that benefits from caching declares its own ``Cache-Control``:
    the live read endpoints call ``app.cache.apply_live_cache_control``
    (auth-aware split: CDN-cached for viewers, ``private, no-store`` for
    authenticated callers); other routes set an explicit header inline.
    Any 200 GET that stays silent falls through to ``no-store`` here.

    The default is deliberately safe rather than fast. The previous
    default — ``public, max-age=3600`` on every cacheless GET — silently
    made auth-gated endpoints publicly cacheable, which caused a string
    of read-after-write and cross-user-cache bugs (#101, #104, #105).
    With ``no-store`` as the floor, an endpoint that forgets to declare a
    cache posture is merely uncached (correct, just not coalesced) rather
    than wrongly shared or served stale. Endpoints that want edge
    coalescing must opt in — and in doing so, think about the
    viewer-vs-admin split explicitly.
    """
    response = await call_next(request)
    if (
        request.method == "GET"
        and response.status_code == 200
        and "cache-control" not in response.headers
    ):
        response.headers["Cache-Control"] = "no-store"
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
    fan_votes_router,
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


# Per RFC 9116 / https://securitytxt.org/ — security researchers and
# automated scanners look for this file to find a disclosure contact.
#
# - `Contact` uses the criticalbit.gg public alias (this is a public
#   surface; the criticalbit.gg domain is the project's public brand).
# - `Canonical` pins this URL as the authoritative location so the file
#   can't be claimed by a third party who served a copy elsewhere.
# - `Expires` must be in the future at all times — bump it before the
#   date below. RFC 9116 recommends keeping it within ~12 months.
SECURITY_TXT = """\
Contact: mailto:security@criticalbit.gg
Expires: 2027-05-30T00:00:00.000Z
Canonical: https://aoe2-live-standings-api.criticalbit.gg/.well-known/security.txt
Preferred-Languages: en
"""


@app.get("/.well-known/security.txt", include_in_schema=False)
@limiter.exempt
async def security_txt() -> PlainTextResponse:
    return PlainTextResponse(SECURITY_TXT)

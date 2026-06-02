"""Sentry SDK initialization.

Must be called *before* the FastAPI app is constructed — the SDK's
Starlette + FastAPI integrations auto-instrument middleware at app
construction time, so a late init misses the request middleware
chain and leaves the request context off captured events.

Empty ``SENTRY_DSN`` is a no-op: dev and tests don't need Sentry, and
prod stays safely Sentry-less until the operator creates a project
and supplies the DSN via Terraform.

The `before_send` hook mirrors `app/logging.py:PII_KEY_PATTERN` so
field names structlog would have redacted from a log line never reach
Sentry either — defence-in-depth against an accidental log payload
containing an Authorization header or an api_key value.
"""

from __future__ import annotations

from typing import Any

import sentry_sdk
from sentry_sdk.integrations.logging import ignore_logger

from app.config import settings
from app.logging import PII_KEY_PATTERN

# Loggers whose records are operational transport noise rather than
# application errors — registered with the Sentry logging integration as
# ignored so they never become Sentry events (#214). Currently just the
# Cloud Trace span exporter, which logs every failed span export at ERROR.
_NOISY_LOGGERS = ("opentelemetry.exporter.cloud_trace",)


def _scrub_pii(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Recursively redact PII-like keys anywhere in a Sentry event.

    Sentry events nest dicts and lists arbitrarily (tags, extra,
    request.headers, request.cookies, breadcrumbs, exception values),
    so the scrubber walks the whole tree. A key whose name matches the
    PII pattern has its value replaced with ``[REDACTED]``; recursion
    continues into non-matching keys' values.
    """

    def _scrub(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                k: "[REDACTED]" if PII_KEY_PATTERN.search(str(k)) else _scrub(v)
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [_scrub(item) for item in obj]
        return obj

    return _scrub(event)


def init_sentry() -> None:
    """Initialize the Sentry SDK. No-op when ``SENTRY_DSN`` is empty.

    Sampling rationale: 100% errors regardless. Traces are sampled at
    10% in prod (volume control) and 100% in dev (so a local request
    always has a trace). Profiling on with ``trace`` lifecycle, which
    only profiles when a trace is sampled.

    ``send_default_pii=False`` is deliberate — at this service's
    layer the only "user" identifier is the opaque criticalbit UUID
    from the JWT ``sub`` claim, which isn't PII; actual PII lives
    in criticalbit-auth-api. If this service ever serves PII
    directly (e.g., email addresses on the read API), revisit.
    """
    if not settings.sentry_dsn:
        return

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        traces_sample_rate=1.0 if settings.is_development else 0.1,
        profile_session_sample_rate=1.0,
        profile_lifecycle="trace",
        send_default_pii=False,
        before_send=_scrub_pii,
        # Bring structlog ERROR-level entries (e.g., `poll_<task>_failed`)
        # into Sentry alongside the exceptions auto-captured from
        # FastAPI handlers and lifespan tasks.
        enable_logs=True,
    )

    # Keep operational transport noise out of Sentry. The Cloud Trace span
    # exporter logs at ERROR on every failed BatchWriteSpans — e.g.
    # RESOURCE_EXHAUSTED once launch-traffic span volume exceeds the Cloud
    # Trace write quota — and with enable_logs=True that flooded Sentry with
    # thousands of issues in hours, drowning real errors (#214). A dropped
    # span degrades tracing, not the app, and surfaces via Cloud Trace's own
    # quota metrics instead.
    for logger_name in _NOISY_LOGGERS:
        ignore_logger(logger_name)

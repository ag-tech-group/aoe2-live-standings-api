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

import logging
import re
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration, ignore_logger

from app.config import API_V1_PREFIX, settings
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


# Transient upstream-availability failures from the poller's outbound calls
# to the Relic/worldsedgelink community API. During an upstream outage every
# poll task fails at once and the per-profile fan-out multiplies it; left
# alone, each distinct rendered log line fingerprints into its own Sentry
# issue, exploding a single outage into dozens of escalating issues and
# burning the (un-sampled) error quota — the 2026-06-09 worldsedgelink 502
# storm. We keep capturing them (so the outage stays visible) but pin them
# to one stable fingerprint: one "poller upstream unavailable" issue with a
# rising count, instead of a storm. Only 5xx / connection-level failures
# match — a 4xx (e.g. a stale profile 404ing) keeps its own grouping so a
# genuinely actionable signal isn't swallowed.
_POLLER_LOGGER_PREFIX = "app.poller."
_TRANSIENT_UPSTREAM_RE = re.compile(
    r"\b50[234]\b|Bad Gateway|Service Unavailable|Gateway Time|"
    r"Connect(?:Error|Timeout)|Read(?:Error|Timeout)|PoolTimeout|"
    r"ConnectionError|RemoteProtocolError",
    re.IGNORECASE,
)
_UPSTREAM_UNAVAILABLE_FINGERPRINT = ["poller-upstream-unavailable"]


def _event_text(event: dict[str, Any]) -> str:
    """Best-effort flatten of an event's message text for signature matching.

    Poller errors arrive via the logging integration, so the structlog event
    dict (carrying the logger name + the upstream error string) lands in
    ``logentry``; we also fold in any top-level ``message`` for safety.
    """
    logentry = event.get("logentry") or {}
    parts = (
        logentry.get("message"),
        logentry.get("formatted"),
        event.get("message"),
    )
    return " ".join(p for p in parts if isinstance(p, str))


def _group_transient_upstream_errors(event: dict[str, Any]) -> None:
    """Collapse poller transient-upstream failures under one fingerprint.

    Mutates ``event`` in place. A no-op for everything else — non-poller
    events and real poller bugs (parse errors, logic errors) keep Sentry's
    default grouping so they surface distinctly.
    """
    text = _event_text(event)
    logger_name = event.get("logger") or ""
    from_poller = logger_name.startswith(_POLLER_LOGGER_PREFIX) or _POLLER_LOGGER_PREFIX in text
    if from_poller and _TRANSIENT_UPSTREAM_RE.search(text):
        event["fingerprint"] = list(_UPSTREAM_UNAVAILABLE_FINGERPRINT)


def _before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """The ``before_send`` hook: group transient upstream noise, then scrub PII.

    Grouping runs first so the fingerprint is set before the (returned)
    scrubbed event is handed back to the SDK.
    """
    _group_transient_upstream_errors(event)
    return _scrub_pii(event, hint)


# Prod trace sample rates. Errors are always captured at 100% regardless
# of these (Sentry handles error sampling independently of traces).
_DEFAULT_TRACES_SAMPLE_RATE = 0.03
_LOW_VALUE_TRACES_SAMPLE_RATE = 0.01

# High-frequency, low-signal request paths sampled below the default rate
# so they don't dominate the monthly Sentry spans budget:
#   - "/" and "/health": container liveness/info probes.
#   - f"{API_V1_PREFIX}/stream": the long-lived SSE nudge stream — each
#     (re)connect is its own transaction across ~thousands of seats, but a
#     content-free nudge stream carries no perf signal worth tracing at the
#     default rate.
# Defined here rather than imported from app.main to avoid a circular import:
# app.main calls init_sentry() at module load, before its own _HEALTH_PATHS.
_LOW_VALUE_TRACE_PATHS = frozenset(("/", "/health", f"{API_V1_PREFIX}/stream"))


def _traces_sampler(sampling_context: dict[str, Any]) -> float:
    """Per-transaction trace sample rate (the ``traces_sampler`` hook).

    Development traces everything so a local request always has a trace.
    In prod, the high-frequency/low-signal paths in
    ``_LOW_VALUE_TRACE_PATHS`` sample at the reduced rate and everything
    else at the default — a flat ``traces_sample_rate`` would spend the
    bulk of the monthly spans budget on probe and SSE-reconnect noise.

    Sampling is decided independently per service: an inbound
    ``sentry-trace`` parent decision is intentionally NOT inherited, so
    the API down-samples its own root transactions regardless of any
    upstream FE sampling. Revisit only if end-to-end FE→API trace
    completeness ever outweighs the volume saving.
    """
    if settings.is_development:
        return 1.0
    scope = sampling_context.get("asgi_scope")
    path = scope.get("path", "") if hasattr(scope, "get") else ""
    if path in _LOW_VALUE_TRACE_PATHS:
        return _LOW_VALUE_TRACES_SAMPLE_RATE
    return _DEFAULT_TRACES_SAMPLE_RATE


def init_sentry() -> None:
    """Initialize the Sentry SDK. No-op when ``SENTRY_DSN`` is empty.

    Sampling rationale: 100% errors regardless. Traces go through
    ``_traces_sampler`` — 100% in dev (so a local request always has a
    trace), and in prod the default rate with high-frequency/low-signal
    paths (health probes, the SSE stream) cut further to keep the
    monthly Sentry spans budget in check. Profiling on with ``trace``
    lifecycle, which only profiles when a trace is sampled.

    Logs: ``enable_logs`` ships structlog records to the Sentry Logs
    product, floored at WARNING (``sentry_logs_level``) so the INFO
    firehose doesn't drain the separately-metered logs budget.
    ``before_send`` groups transient upstream-availability noise under one
    fingerprint (so an upstream outage is one visible issue, not a storm)
    before the PII scrub.

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
        traces_sampler=_traces_sampler,
        profile_session_sample_rate=1.0,
        profile_lifecycle="trace",
        send_default_pii=False,
        before_send=_before_send,
        # Bring structlog ERROR-level entries (e.g., `poll_<task>_failed`)
        # into Sentry alongside the exceptions auto-captured from
        # FastAPI handlers and lifespan tasks.
        enable_logs=True,
        # Forward only WARNING+ records to the Sentry Logs product (the paid,
        # separately-metered logs stream). `event_level` stays at its default
        # (ERROR) so error capture is unchanged; this only lifts the floor on
        # the high-volume INFO firehose (httpx request lines, per-request
        # access logs, `poll_*_ok`) that was driving the monthly logs budget
        # toward its cap. The full INFO stream still reaches stdout ->
        # Cloud Logging. Passing the instance overrides only the logging
        # integration's config; the other auto-enabled integrations
        # (FastAPI, Starlette, asyncio) stay on.
        integrations=[LoggingIntegration(sentry_logs_level=logging.WARNING)],
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

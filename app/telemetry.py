"""OpenTelemetry tracing — FastAPI + SQLAlchemy + asyncpg + httpx.

Off by default (`otel_enabled=False` in `Settings`); when enabled,
traces from request handlers, database queries, and upstream HTTP
calls are exported via OTLP/gRPC or directly to Cloud Trace (selected
by `otel_use_cloud_trace`).

In prod, both Cloud Run services run with `OTEL_ENABLED=true`,
`OTEL_USE_CLOUD_TRACE=true`, and `OTEL_TRACES_SAMPLE_RATIO=0.1` (10%
sampling — Cloud Trace bills per span, and 10% over the API's traffic
is more than enough signal for the typical "why was this request
slow" investigation). Errors are not gated by trace sampling — they're
captured by Sentry independently.

The Cloud Trace path needs the runtime SA to hold
`roles/cloudtrace.agent`, granted in `infra/terraform/iam.tf`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.config import settings

if TYPE_CHECKING:
    from fastapi import FastAPI


def setup_telemetry(app: FastAPI) -> None:
    """Wire up OpenTelemetry tracing. No-op when `otel_enabled` is False.

    Auto-instruments FastAPI's request handlers, SQLAlchemy's engines,
    asyncpg's connections, and httpx's client — so every database
    query and upstream HTTP call shows as a child span under the
    request span, with no per-call instrumentation needed in
    application code.
    """
    if not settings.otel_enabled:
        return

    from opentelemetry import trace
    from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

    resource = Resource.create({"service.name": settings.otel_service_name})
    provider = TracerProvider(
        resource=resource,
        sampler=TraceIdRatioBased(settings.otel_traces_sample_ratio),
    )

    if settings.otel_use_cloud_trace:
        # Cloud Trace exporter — auth via the Cloud Run runtime SA
        # (no explicit credentials), project inferred from the GCP
        # metadata server.
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

        exporter: object = CloudTraceSpanExporter()
    else:
        # OTLP/gRPC fallback — for self-hosted collectors (Tempo,
        # Jaeger, etc.) or local dev.
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_endpoint)

    provider.add_span_processor(BatchSpanProcessor(exporter))  # type: ignore[arg-type]
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
    # SQLAlchemy + asyncpg are imported by `app.database` at module
    # load; instrumentation hooks them at this point, after the
    # provider is set so spans go to the right exporter.
    from app.database import engine

    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
    AsyncPGInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()

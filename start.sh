#!/bin/sh
set -e

# Migrations are no longer run here — they run as a Cloud Run Job
# (`aoe2-live-standings-api-migrate`) that completes before any new
# service revision rolls. See `infra/terraform/jobs.tf` and the
# `Run database migrations` step in `.github/workflows/ci.yml`.
# Keeping migrations out of the container start path avoids the race
# where a new revision rolls out while the old one is still serving
# traffic, and lets a migration failure halt the deploy *before*
# services swap rather than surfacing as a soft "container never
# becomes Ready" signal after the fact.

# Start the application.
# --proxy-headers --forwarded-allow-ips='*' makes uvicorn trust the
# X-Forwarded-For / X-Forwarded-Proto headers set by the upstream proxy
# (Cloud Run, an ALB, nginx, ...), so request.client.host is the real client IP
# rather than the proxy's internal address. The rate limiter keys on that IP, so
# without this every request collapses into one bucket. '*' trusts any upstream —
# correct when the service is ALWAYS behind a proxy that overwrites these headers;
# if it can also be reached directly, restrict this to the proxy's IP range.
exec uv run uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" \
    --proxy-headers --forwarded-allow-ips='*'

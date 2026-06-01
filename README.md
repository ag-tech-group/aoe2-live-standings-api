# AoE2 Live Standings API

[![CI](https://github.com/ag-tech-group/aoe2-live-standings-api/actions/workflows/ci.yml/badge.svg)](https://github.com/ag-tech-group/aoe2-live-standings-api/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.12-blue.svg)](https://www.python.org/)

Open-source live-standings API for AoE2: DE tournaments. One deployment serves multiple tournaments — each a named roster of players on a leaderboard — tracking current ratings, max ratings, recent match history, win/loss streaks, and live-match detection. Brackets and branding stay in consumers; rosters, dates, and teams live here, so every consumer reads consistent, denormalized standings.

The upstream data layer is documented in [`docs/data-sources.md`](docs/data-sources.md).

> Age of Empires II © Microsoft Corporation. AoE2 Live Standings API was created under Microsoft's [Game Content Usage Rules](https://www.xbox.com/en-us/developers/rules) using assets from Age of Empires II and it is not endorsed by or affiliated with Microsoft.

## Table of Contents

- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Running with Docker](#running-with-docker)
- [API Documentation](#api-documentation)
- [API Endpoints](#api-endpoints)
- [Authentication](#authentication)
- [Logging, Telemetry & Feature Flags](#logging-telemetry--feature-flags)
- [Database Migrations](#database-migrations)
- [Testing](#testing)
- [Linting & Formatting](#linting--formatting)
- [Git Setup & Pre-commit Hooks](#git-setup--pre-commit-hooks)
- [Project Structure](#project-structure)
- [Environment Variables](#environment-variables)
- [License](#license)

## Architecture

Two Cloud Run services share one Postgres database and one container image, differentiated only by env vars (`POLLING_ENABLED` / `LISTENER_ENABLED`):

- **worker** — a pinned singleton (`min=max=1`, private — no public traffic). Polls the upstream Relic backend (`aoe-api.worldsedgelink.com/community/*`, see [`docs/data-sources.md`](docs/data-sources.md)) on three cadences (30 s / 60 s / 15 s), writes to Postgres, and emits a `pg_notify` inside the same transaction whenever data changes.
- **api** — autoscaling read tier (`min=1 max=10`, public). Serves the `/v1/*` REST endpoints and the SSE `/v1/stream`. Runs a dedicated `LISTEN` connection that picks up the worker's NOTIFYs and fans nudges to its SSE subscribers.

```
   Relic backend (upstream)
             ▲
             │ poll
             │
   ┌─────────┴────────┐
   │ worker service   │   singleton; writes + pg_notify
   │ (private,        │
   │  min=max=1)      │
   └─────────┬────────┘
             │ write + pg_notify (in transaction)
             ▼
   ┌──────────────────┐
   │     Postgres     │
   │   (snapshot)     │
   └─────────┬────────┘
             │ read + LISTEN
             ▼
   ┌──────────────────┐
   │   api service    │   autoscaled; serves /v1/* + SSE
   │ (public,         │
   │  min=1 max=10)   │
   └─────────┬────────┘
             │ /v1/* + SSE nudges
             ▼
         consumers
        (web client)
```

Reads are denormalized: each response row carries everything a consumer needs to render it, so consumers never fan out or join across endpoints.

In local development (and tests) both flags default true, so a single uvicorn process runs everything — mono mode. Tests bypass the lifespan entirely via `ASGITransport`.

## Tech Stack

| Component          | Technology                                                                 |
| ------------------ | -------------------------------------------------------------------------- |
| Framework          | [FastAPI](https://fastapi.tiangolo.com/)                                   |
| Database           | [PostgreSQL](https://www.postgresql.org/) (async via [asyncpg](https://magicstack.github.io/asyncpg/)) |
| ORM                | [SQLAlchemy 2.0](https://www.sqlalchemy.org/)                              |
| Migrations         | [Alembic](https://alembic.sqlalchemy.org/)                                 |
| Rate Limiting      | [slowapi](https://slowapi.readthedocs.io/)                                 |
| Logging            | [structlog](https://www.structlog.org/)                                    |
| Telemetry          | [OpenTelemetry](https://opentelemetry.io/)                                 |
| Package Manager    | [uv](https://docs.astral.sh/uv/)                                           |
| Containerization   | [Docker](https://www.docker.com/) / [Docker Compose](https://docs.docker.com/compose/) |
| Testing            | [Pytest](https://docs.pytest.org/) (async via [pytest-asyncio](https://pytest-asyncio.readthedocs.io/)) |
| Linting/Formatting | [Ruff](https://docs.astral.sh/ruff/)                                       |
| Git Hooks          | [pre-commit](https://pre-commit.com/)                                      |

## Requirements

- Python 3.12+
- uv
- Docker & Docker Compose (for local development)

## Quick Start

```bash
# Copy environment file
cp .env.example .env

# Install dependencies
uv sync

# Start PostgreSQL
docker compose up -d db

# Run migrations
uv run alembic upgrade head

# Start the API
uv run uvicorn app.main:app --reload
```

The API will be available at http://localhost:8000.

## Running with Docker

```bash
docker compose up        # foreground (API + PostgreSQL + Adminer)
docker compose up -d     # detached
```

Adminer is available at http://localhost:8080. Login: System=PostgreSQL, Server=db, User=postgres, Password=postgres, Database=aoe2_live_standings.

## API Documentation

Once running, visit:

- Scalar UI: http://localhost:8000/docs
- OpenAPI JSON: http://localhost:8000/openapi.json

The OpenAPI spec is consumed by the companion consumer projects (see e.g. [`hera-streamer-invitational-2026-web`](https://github.com/ag-tech-group/hera-streamer-invitational-2026-web)) via [orval](https://orval.dev/) to generate type-safe React Query hooks. Run the generator from the consumer side after starting this API locally (or after pointing at a deployed instance).

## API Endpoints

All application routes are served under the `/v1` prefix so the API surface can be versioned as a whole. Infrastructure routes (`/`, `/health`, `/docs`, `/openapi.json`, `/.well-known/security.txt`) stay unversioned. Routers are registered in the `ROUTERS` tuple in `app/main.py`, which loop-mounts each one with the `/v1` prefix.

### Read endpoints (public)

Most of the API is scoped to a tournament:

- `GET /v1/tournaments` — list tournaments; `GET /v1/tournaments/{slug}` — one tournament
- `GET /v1/tournaments/{slug}/standings` — the tournament's standings
- `GET /v1/tournaments/{slug}/teams/standings` — the tournament's team standings
- `GET /v1/tournaments/{slug}/matches`, `.../matches/{match_id}` — match feed and detail
- `GET /v1/tournaments/{slug}/live` — the roster's live matches
- `GET /v1/tournaments/{slug}/players`, `.../players/{profile_id}` — roster and player detail

Unscoped: `GET /v1/leaderboards` (leaderboard metadata), `GET /v1/stream` (SSE refresh nudges), `GET /v1/flags` (feature flags).

### Authenticated read

- `GET /v1/me` — identity (`user_id`) plus the list of tournaments the caller owns. One round-trip lets the frontend gate admin UI without per-tournament probes. 401 when unauthenticated.

### Write endpoints (authenticated)

The management API lets a tournament host edit configuration without a redeploy. Every write route is gated — see [Authentication](#authentication). Writes accept an optional `Idempotency-Key: <uuid>` header to dedupe retries (same key + same body → cached response).

- `POST /v1/tournaments` — create a tournament. Any authenticated user may; the caller becomes the first owner. `DELETE /v1/tournaments/{slug}` — delete the tournament and everything tournament-scoped (cascades to roster, teams, owners).
- `PATCH /v1/tournaments/{slug}` — edit a tournament's name, dates, or leaderboard
- `GET /v1/tournaments/{slug}/owners` — list owners; `POST` to grant ownership to another criticalbit user; `DELETE .../owners/{user_id}` to revoke. Revoking the last owner is rejected (the tournament would become uneditable).
- `POST /v1/tournaments/{slug}/players` — add a profile to the roster; `DELETE .../players/{profile_id}` — remove one
- `POST /v1/tournaments/{slug}/teams` — create a team; `PATCH` / `DELETE .../teams/{team_id}` — edit or delete one
- `POST /v1/tournaments/{slug}/teams/{team_id}/members` — add a team member; `DELETE .../members/{profile_id}` — remove one

See `/docs` or `/openapi.json` for the full, authoritative spec.

## Authentication

Reads are public. The write/management API is authenticated against [criticalbit-auth-api](https://github.com/ag-tech-group/criticalbit-auth-api), the shared criticalbit.gg SSO service:

- **Authentication** — a write request must carry a valid `criticalbit_access` cookie (an RS256 JWT issued by criticalbit-auth-api). The API verifies it against that service's public JWKS endpoint (`AUTH_JWKS_URL`); a missing or invalid token is a `401`.
- **Authorization** — a verified token identifies a criticalbit user. To edit a tournament, that user must have a row in this service's `tournament_owners` table for it, or the request is a `403`. Ownership is per-tournament and modelled here — not in the auth service, which deliberately stays free of app-specific roles.

Owner rows are inserted directly (SQL) for now; an API to grant and revoke ownership is planned. A roster edited through this API is picked up by the polling worker on its next cycle, with no redeploy.

## Logging, Telemetry & Feature Flags

### Structured Logging

Logging uses [structlog](https://www.structlog.org/) for structured output. In development you get colored console logs; in production, JSON.

Every request is assigned a unique `X-Request-ID` header (or reuses one from the incoming request), and it's automatically bound to all log entries for that request.

Configure via `LOG_LEVEL` env var (default: `INFO`).

### OpenTelemetry

OpenTelemetry tracing is included but disabled by default. To enable, set `OTEL_ENABLED=true` and point `OTEL_EXPORTER_ENDPOINT` at your collector (e.g. Jaeger, Grafana Tempo). FastAPI is auto-instrumented — no code changes needed.

### Monitoring

Cloud Monitoring covers the prod deployment. Alert policies live in [`infra/terraform/monitoring.tf`](infra/terraform/monitoring.tf) (poller silent-failure, upstream rate-limit) and [`infra/terraform/capacity_alerts.tf`](infra/terraform/capacity_alerts.tf) (Cloud Run concurrency, SQL CPU/connections).

A single-pane event-day dashboard (request rate, latency percentiles, instance counts, Postgres connections + CPU, poller per-task ok rate, upstream 429s) is defined in [`infra/terraform/dashboard.tf`](infra/terraform/dashboard.tf) and lives at [console.cloud.google.com/monitoring/dashboards/builder/8926650c-e0a2-45e6-bb1a-d2f0d02f04bc](https://console.cloud.google.com/monitoring/dashboards/builder/8926650c-e0a2-45e6-bb1a-d2f0d02f04bc?project=aoe2-live-standings-api). The deploy also outputs this URL as `event_day_dashboard_url` so it survives a destroy-recreate without manual lookup.

### Feature Flags

Feature flags are read from `FEATURE_*` environment variables at startup (no database required). Set `FEATURE_<NAME>=true` or `false` in your `.env`.

The `GET /v1/flags` endpoint returns all flags as a JSON object.

Use the `get_feature_flags()` dependency in route handlers to check flags server-side via `flags.is_enabled("flag_name")`.

## Database Migrations

This project uses Alembic for database migrations.

### Workflow

1. Edit a model in `app/models/`
2. Make sure the model is imported in `alembic/env.py` (so autogenerate can detect it)
3. Generate a migration:
   ```bash
   uv run alembic revision --autogenerate -m "description of change"
   ```
4. Review the generated file in `alembic/versions/` (autogenerate can miss some changes)
5. Apply the migration:
   ```bash
   uv run alembic upgrade head
   ```
6. Commit both the model change and the migration file

### Common Commands

```bash
# Apply all pending migrations
uv run alembic upgrade head

# Rollback one migration
uv run alembic downgrade -1

# See current migration status
uv run alembic current

# See migration history
uv run alembic history

# Generate a migration without applying
uv run alembic revision --autogenerate -m "description"
```

## Testing

Tests use SQLite in-memory for speed and isolation.

```bash
# Run all tests
uv run pytest

# Verbose
uv run pytest -v

# With coverage
uv run pytest --cov=app
```

The test harness provides a `client` fixture (an unauthenticated async HTTP client), a `session` fixture (a direct async SQLAlchemy session for test setup), and an `auth_as` fixture that authenticates the client as a given user for write-endpoint tests.

## Linting & Formatting

This project uses [Ruff](https://docs.astral.sh/ruff/) for linting and formatting.

```bash
# Lint
uv run ruff check .

# Auto-fix
uv run ruff check --fix .

# Format
uv run ruff format .

# Check formatting without changes
uv run ruff format --check .
```

## Git Setup & Pre-commit Hooks

Install pre-commit hooks so ruff runs automatically on every commit:

```bash
uv run pre-commit install
uv run pre-commit run --all-files  # one-time run across the repo
```

## Project Structure

```
aoe2-live-standings-api/
├── app/
│   ├── auth/                  # JWT verification + tournament-owner authorization
│   ├── models/                # SQLAlchemy models
│   ├── routers/               # FastAPI routers, mounted under /v1
│   ├── schemas/               # Pydantic request/response schemas
│   ├── config.py              # Settings (env-backed) + production validation
│   ├── database.py            # Async SQLAlchemy setup
│   ├── features.py            # Feature flags (env-var backed) + /v1/flags
│   ├── logging.py             # structlog configuration
│   ├── telemetry.py           # OpenTelemetry setup
│   └── main.py                # App entry point, middleware, infra routes
├── alembic/
│   ├── versions/              # Migration files
│   └── env.py                 # Alembic configuration
├── docs/
│   └── data-sources.md        # Upstream data source notes
├── tests/
│   ├── conftest.py            # Fixtures (client, session)
│   └── test_app.py            # security.txt + rate-limit-exemption tests
├── .env.example
├── .pre-commit-config.yaml
├── .python-version            # pyenv Python version
├── docker-compose.yml         # API + PostgreSQL + Adminer
├── Dockerfile
└── pyproject.toml
```

## Environment Variables

"Required" means the value **must be set when `ENVIRONMENT=production`** — production config validation rejects defaults for these. Local development runs out of the box with no env vars set.

| Variable                 | Required | Description                                       | Default                                                                     |
| ------------------------ | -------- | ------------------------------------------------- | --------------------------------------------------------------------------- |
| `ENVIRONMENT`            | Optional | `development` or `production`                     | `development`                                                               |
| `DATABASE_URL`           | Required | PostgreSQL connection string                      | `postgresql+asyncpg://postgres:postgres@localhost:5432/aoe2_live_standings` |
| `CORS_ORIGINS`           | Required | Comma-separated allowed origins                   | (empty — dev uses `localhost:5100-5199`)                                    |
| `LOG_LEVEL`              | Optional | Logging level                                     | `INFO`                                                                      |
| `OTEL_ENABLED`           | Optional | Enable OpenTelemetry tracing                      | `false`                                                                     |
| `OTEL_SERVICE_NAME`      | Optional | Service name for traces                           | `aoe2-live-standings-api`                                                   |
| `OTEL_EXPORTER_ENDPOINT` | Optional | OTLP gRPC collector endpoint (used when `OTEL_USE_CLOUD_TRACE` is false) | `http://localhost:4317`                              |
| `OTEL_USE_CLOUD_TRACE`   | Optional | Export spans directly to Google Cloud Trace via the native exporter (prod) | `false`                                            |
| `OTEL_TRACES_SAMPLE_RATIO` | Optional | Fraction of incoming traces to sample (1.0 = 100%, 0.1 = 10%) | `1.0`                                                          |
| `SENTRY_DSN`             | Optional | Sentry project DSN. Empty disables Sentry init entirely | (empty)                                                              |
| `FEATURE_*`              | Optional | Feature flags (e.g. `FEATURE_ERROR_ENVELOPE_V2=true`) | (none)                                                                  |
| `POLLING_ENABLED`        | Optional | Start the three upstream pollers in this process (worker service) | `true`                                                  |
| `LISTENER_ENABLED`       | Optional | Start the LISTEN/NOTIFY consumer in this process (api service)    | `true`                                                  |
| `UPSTREAM_BASE_URL`      | Optional | Relic upstream base URL                           | `https://aoe-api.worldsedgelink.com`                                        |
| `AUTH_JWKS_URL`          | Optional | JWKS endpoint used to verify the write API's access tokens | `https://auth-api.criticalbit.gg/auth/jwks` |
| `AUTH_TOKEN_ISSUER`      | Optional | Expected JWT `iss` claim; when set, tokens with a different issuer are rejected | (empty — issuer not checked) |

Before deploying to production, replace the placeholder `Contact:` in the `SECURITY_TXT` constant (`app/main.py`) with a real security-disclosure address and bump `Expires:` if it's close.

## License

Apache 2.0 — see [LICENSE](LICENSE).

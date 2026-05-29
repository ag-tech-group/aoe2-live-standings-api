from datetime import datetime

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Prefix under which every application route is mounted, so the whole API
# surface is versioned together. Infrastructure routes (/, /health, /docs,
# /openapi.json, /.well-known/security.txt) stay unversioned. Not env-
# overridable on purpose — the path version is part of the code contract,
# not deployment config.
API_V1_PREFIX = "/v1"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/aoe2_live_standings"

    environment: str = "development"

    cors_origins: str = ""

    log_level: str = "INFO"

    otel_enabled: bool = False
    otel_service_name: str = "aoe2-live-standings-api"
    otel_exporter_endpoint: str = "http://localhost:4317"
    # When otel_enabled=True, what fraction of incoming traces to
    # sample. 1.0 in dev so every local request is traceable; 0.1 in
    # prod via env var, since trace volume directly drives cost on
    # the Cloud Trace side.
    otel_traces_sample_ratio: float = 1.0
    # If true, the Cloud Trace exporter is used (auth via the Cloud
    # Run runtime SA, project inferred from metadata). If false, the
    # OTLP/gRPC exporter is used (for self-hosted collectors or
    # other backends — keeps the legacy path working).
    otel_use_cloud_trace: bool = False

    # Sentry DSN — empty disables Sentry initialization entirely (the
    # default in dev and tests, and the safe-state if the operator
    # hasn't created a Sentry project yet). In prod, Cloud Run mounts
    # the value from the `sentry-dsn` Secret Manager secret as the
    # `SENTRY_DSN` env var (see infra/terraform/secrets.tf + run.tf).
    sentry_dsn: str = ""

    # When True, HTTPException + RequestValidationError responses use
    # the new envelope shape from `app/errors.py` (#57). When False
    # (the default during the rollout window), they keep FastAPI's
    # legacy `{"detail": ...}` shape. `BusinessError` always uses the
    # envelope regardless of this flag. Flip on after the frontend
    # has updated its error-handling to read the envelope shape, then
    # delete this flag + the legacy paths in a follow-up.
    feature_error_envelope_v2: bool = False

    # Upstream polling configuration.
    upstream_base_url: str = "https://aoe-api.worldsedgelink.com"
    polling_enabled: bool = True

    # Whether the LISTEN/NOTIFY listener runs in this process. In prod
    # the API service has this true and the worker has it false — the
    # worker writes nudges but has no SSE clients to fan out to. Local
    # dev defaults both true so a single mono process does everything.
    listener_enabled: bool = True

    # Seed-tournament bootstrap. On startup, when the `tournaments` table is
    # empty, a tournament is created from these values with the
    # `tracked_profile_ids` roster — the migration path off the old
    # single-deployment config and the zero-touch seed for a fresh deploy.
    # Once any tournament exists the bootstrap is a no-op.
    tracked_profile_ids: str = ""
    tournament_slug: str = "default"
    tournament_name: str = "Default Tournament"
    tournament_leaderboard_id: int = 3
    tournament_start_date: datetime | None = None
    tournament_grand_finals_date: datetime | None = None

    # Authentication for the write/management API. The read surface stays
    # unauthenticated; write routes verify the `criticalbit_access` cookie's
    # RS256 JWT against criticalbit-auth-api's public JWKS. `auth_token_issuer`
    # is enforced as the expected `iss` claim only when set (empty = skip).
    auth_jwks_url: str = "https://auth-api.criticalbit.gg/auth/jwks"
    auth_token_issuer: str = ""
    # Base URL for criticalbit-auth-api outbound calls (user identity lookups).
    # Defaults to the same prod host as the JWKS URL; override in local dev
    # to point at a locally-run auth-api on another port.
    auth_api_base_url: str = "https://auth-api.criticalbit.gg"

    # Twitch broadcast-live detection (#112). Both empty disables it
    # entirely — the safe default in dev/test and before the Twitch app
    # exists. The client id is not sensitive (Twitch exposes it in
    # client-side calls); the secret is supplied in prod from the
    # `twitch-client-secret` Secret Manager secret, mirroring `sentry_dsn`.
    twitch_client_id: str = ""
    twitch_client_secret: str = ""

    # YouTube broadcast-live detection (#112). Empty disables it. Quota is
    # the real constraint, not the credential: the Data API's free 10k
    # units/day vs. search.list at 100 units/call means only a handful of
    # channels can be polled, on a slow cadence (see run_youtube_live_poller).
    # Supplied in prod from the `youtube-api-key` Secret Manager secret.
    youtube_api_key: str = ""

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def cors_origin_list(self) -> list[str]:
        if self.is_development:
            return [f"http://localhost:{p}" for p in range(5100, 5200)]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def cors_origin_regex(self) -> str | None:
        """Allowed-origin regex, layered on top of ``cors_origin_list``.

        In production every ``*.criticalbit.gg`` subdomain is allowed so
        the companion management app — which calls the write API with the
        ``criticalbit_access`` cookie — is accepted without enumerating
        each tool's origin. ``None`` in development, where the localhost
        port range in ``cors_origin_list`` already covers local apps.
        """
        if self.is_development:
            return None
        return r"https://([a-z0-9-]+\.)?criticalbit\.gg"

    @property
    def tracked_profile_id_list(self) -> list[int]:
        """Parse the CSV ``TRACKED_PROFILE_IDS`` env var into ints, skipping blanks."""
        return [int(p.strip()) for p in self.tracked_profile_ids.split(",") if p.strip()]

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        """Fail-loud guard against shipping common misconfig to prod.

        Catches the cases where a default placeholder value or a known
        weak credential would otherwise silently make it into a non-
        development environment. Cheap insurance against an env-var
        added in a follow-up PR that ships with its placeholder.
        """
        if self.is_development:
            return self

        if "postgres:postgres@" in self.database_url:
            raise ValueError(
                "Default database credentials (postgres:postgres) must not be used "
                "outside development — set DATABASE_URL to a real connection string"
            )

        # The default JWKS URL is the prod criticalbit-auth-api host;
        # an explicit empty value means the JWT verifier has nothing
        # to verify against and every request would 401. Catch that
        # at boot rather than at first request.
        if not self.auth_jwks_url:
            raise ValueError(
                "AUTH_JWKS_URL must not be empty outside development — the "
                "write/management API can't verify access tokens without it"
            )

        # Owner-list identity enrichment calls auth-api at this base; empty
        # would silently degrade every enriched response. Same fail-loud
        # treatment as the JWKS URL.
        if not self.auth_api_base_url:
            raise ValueError(
                "AUTH_API_BASE_URL must not be empty outside development — the "
                "owners-list endpoint resolves user identity against it"
            )

        return self


settings = Settings()

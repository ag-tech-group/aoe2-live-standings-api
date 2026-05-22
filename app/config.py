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

    # Upstream polling configuration.
    upstream_base_url: str = "https://aoe-api.worldsedgelink.com"
    polling_enabled: bool = True

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
    tournament_end_date: datetime | None = None

    # Authentication for the write/management API. The read surface stays
    # unauthenticated; write routes verify the `criticalbit_access` cookie's
    # RS256 JWT against criticalbit-auth-api's public JWKS. `auth_token_issuer`
    # is enforced as the expected `iss` claim only when set (empty = skip).
    auth_jwks_url: str = "https://auth-api.criticalbit.gg/auth/jwks"
    auth_token_issuer: str = ""

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
        if not self.is_development:
            if "postgres:postgres@" in self.database_url:
                raise ValueError("Default database credentials must not be used in production")
        return self


settings = Settings()

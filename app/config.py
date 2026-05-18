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

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def cors_origin_list(self) -> list[str]:
        if self.is_development:
            return [f"http://localhost:{p}" for p in range(5100, 5200)]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        if not self.is_development:
            if "postgres:postgres@" in self.database_url:
                raise ValueError("Default database credentials must not be used in production")
        return self


settings = Settings()

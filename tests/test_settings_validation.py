"""Tests for `Settings.validate_production_settings` — guards against
shipping common misconfig to prod (#59)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


class TestProductionSettingsValidation:
    """The validator only runs outside development."""

    def test_default_db_credentials_in_production_raises(self):
        with pytest.raises(ValidationError) as exc:
            Settings(
                environment="production",
                database_url="postgresql+asyncpg://postgres:postgres@host/db",
                auth_jwks_url="https://auth.example.com/jwks",
            )
        # The error message names the offending setting, not just "validation
        # failed" — so an operator sees what to fix at a glance.
        assert "DATABASE_URL" in str(exc.value) or "credentials" in str(exc.value)

    def test_default_db_credentials_in_development_pass(self):
        # In dev the postgres:postgres@ default is the expected docker-
        # compose / local-dev setup. The validator must not block it.
        s = Settings(
            environment="development",
            database_url="postgresql+asyncpg://postgres:postgres@localhost/db",
            auth_jwks_url="",
        )
        assert s.is_development

    def test_empty_jwks_url_in_production_raises(self):
        with pytest.raises(ValidationError) as exc:
            Settings(
                environment="production",
                database_url="postgresql+asyncpg://user:strongpass@host/db",
                auth_jwks_url="",
            )
        assert "AUTH_JWKS_URL" in str(exc.value) or "JWKS" in str(exc.value)

    def test_real_production_config_passes(self):
        # A realistic prod-shaped config should construct cleanly.
        s = Settings(
            environment="production",
            database_url="postgresql+asyncpg://app_user:strongsecret@/db?host=/cloudsql/proj:reg:inst",
            auth_jwks_url="https://auth-api.criticalbit.gg/auth/jwks",
        )
        assert not s.is_development

    def test_connector_without_components_in_production_raises(self):
        # DB_USE_CONNECTOR with no instance/user/password can't reach the
        # pooler — catch the half-configured rollout at boot (#196).
        with pytest.raises(ValidationError) as exc:
            Settings(
                environment="production",
                database_url="postgresql+asyncpg://app_user:strongsecret@/db?host=/cloudsql/p:r:i",
                auth_jwks_url="https://auth-api.criticalbit.gg/auth/jwks",
                db_use_connector=True,
            )
        assert "DB_USE_CONNECTOR" in str(exc.value) or "connector" in str(exc.value)

    def test_connector_with_components_in_production_passes(self):
        # Fully-specified connector config (the prod api service) constructs.
        s = Settings(
            environment="production",
            database_url="postgresql+asyncpg://app_user:strongsecret@/db?host=/cloudsql/p:r:i",
            auth_jwks_url="https://auth-api.criticalbit.gg/auth/jwks",
            db_use_connector=True,
            db_instance_connection_name="proj:region:inst",
            db_user="app_user",
            db_password="strongsecret",
        )
        assert s.db_use_connector

    def test_connector_flag_off_skips_component_check(self):
        # The default (direct DSN) path needs no connector components.
        s = Settings(
            environment="production",
            database_url="postgresql+asyncpg://app_user:strongsecret@/db?host=/cloudsql/p:r:i",
            auth_jwks_url="https://auth-api.criticalbit.gg/auth/jwks",
        )
        assert not s.db_use_connector

"""Tests for app/sentry.py — init no-op gating + before_send PII scrubbing."""

from __future__ import annotations

from typing import Any

import pytest

from app.sentry import _scrub_pii, init_sentry


class TestInitSentry:
    """`init_sentry()` gates on the `SENTRY_DSN` setting."""

    def test_noop_when_dsn_empty(self, monkeypatch: pytest.MonkeyPatch):
        # Default settings in tests have no DSN — the SDK init call must
        # be skipped entirely (so tests don't accidentally send events).
        monkeypatch.setattr("app.sentry.settings.sentry_dsn", "")
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr("sentry_sdk.init", lambda **kwargs: calls.append(kwargs))

        init_sentry()

        assert calls == [], "sentry_sdk.init must not be invoked when DSN is empty"

    def test_initializes_when_dsn_set(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("app.sentry.settings.sentry_dsn", "https://abc@o.example.com/1")
        monkeypatch.setattr("app.sentry.settings.environment", "production")
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr("sentry_sdk.init", lambda **kwargs: calls.append(kwargs))

        init_sentry()

        assert len(calls) == 1
        kwargs = calls[0]
        assert kwargs["dsn"] == "https://abc@o.example.com/1"
        assert kwargs["environment"] == "production"
        # Prod sampling: 10% traces, 100% errors (which Sentry handles
        # via its own internal sampling regardless of trace rate).
        assert kwargs["traces_sample_rate"] == 0.1
        # PII off by default at this layer (only opaque UUIDs).
        assert kwargs["send_default_pii"] is False
        assert kwargs["before_send"] is _scrub_pii

    def test_dev_environment_samples_traces_fully(self, monkeypatch: pytest.MonkeyPatch):
        # Development gets 100% trace sampling so every local request
        # has a trace to inspect; volume isn't a concern.
        monkeypatch.setattr("app.sentry.settings.sentry_dsn", "https://abc@o.example.com/1")
        monkeypatch.setattr("app.sentry.settings.environment", "development")
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr("sentry_sdk.init", lambda **kwargs: calls.append(kwargs))

        init_sentry()

        assert calls[0]["traces_sample_rate"] == 1.0

    def test_ignores_cloud_trace_exporter_logger(self, monkeypatch: pytest.MonkeyPatch):
        # The Cloud Trace span exporter logs at ERROR on every failed
        # BatchWriteSpans (e.g. RESOURCE_EXHAUSTED once span volume exceeds
        # the Cloud Trace quota); with enable_logs=True those flooded Sentry
        # with thousands of issues, drowning real errors (#214). init must
        # register that logger as ignored so its transport noise never
        # becomes a Sentry event.
        monkeypatch.setattr("app.sentry.settings.sentry_dsn", "https://abc@o.example.com/1")
        monkeypatch.setattr("sentry_sdk.init", lambda **kwargs: None)
        ignored: list[str] = []
        monkeypatch.setattr("app.sentry.ignore_logger", lambda name: ignored.append(name))

        init_sentry()

        assert "opentelemetry.exporter.cloud_trace" in ignored

    def test_does_not_register_ignores_when_dsn_empty(self, monkeypatch: pytest.MonkeyPatch):
        # No DSN → no init, so there's nothing to ignore either.
        monkeypatch.setattr("app.sentry.settings.sentry_dsn", "")
        ignored: list[str] = []
        monkeypatch.setattr("app.sentry.ignore_logger", lambda name: ignored.append(name))

        init_sentry()

        assert ignored == []


class TestScrubPii:
    """`_scrub_pii` recursively redacts PII-pattern keys anywhere in
    the event tree, mirroring the structlog processor in
    `app/logging.py`."""

    def test_redacts_matching_keys_at_top_level(self):
        event = {"password": "hunter2", "username": "amr"}
        out = _scrub_pii(event, {})
        assert out == {"password": "[REDACTED]", "username": "amr"}

    def test_recurses_into_nested_dicts(self):
        event = {
            "request": {
                "headers": {
                    "Authorization": "Bearer abc",
                    "X-Custom": "ok",
                },
            },
            "extra": {
                "api_key": "secret",
                "user_email": "amr@example.com",
            },
        }
        out = _scrub_pii(event, {})
        assert out["request"]["headers"]["Authorization"] == "[REDACTED]"
        assert out["request"]["headers"]["X-Custom"] == "ok"
        assert out["extra"]["api_key"] == "[REDACTED]"
        # Note: "user_email" doesn't match the PII pattern — it would
        # need to be in the regex for the structlog processor first.
        assert out["extra"]["user_email"] == "amr@example.com"

    def test_recurses_into_lists(self):
        event = {
            "breadcrumbs": [
                {"data": {"cookie": "xyz", "category": "http"}},
                {"data": {"category": "nav"}},
            ],
        }
        out = _scrub_pii(event, {})
        assert out["breadcrumbs"][0]["data"]["cookie"] == "[REDACTED]"
        assert out["breadcrumbs"][0]["data"]["category"] == "http"
        assert out["breadcrumbs"][1]["data"]["category"] == "nav"

    def test_pattern_match_is_case_insensitive(self):
        event = {"AUTHORIZATION": "Bearer", "TOKEN_FRESH": "abc"}
        out = _scrub_pii(event, {})
        assert out["AUTHORIZATION"] == "[REDACTED]"
        assert out["TOKEN_FRESH"] == "[REDACTED]"

    def test_leaves_non_matching_values_intact(self):
        # Values that look sensitive but live under non-matching keys
        # pass through unchanged. Scrubbing is key-name-based, by
        # design — it's defence-in-depth, not full PII inference.
        event = {"message": "user supplied password hunter2"}
        out = _scrub_pii(event, {})
        assert out["message"] == "user supplied password hunter2"

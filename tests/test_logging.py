"""Tests for app/logging.py — third-party logger quieting."""

from __future__ import annotations

import logging

from app.logging import setup_logging


def test_quiets_noisy_third_party_loggers():
    """`setup_logging` floors httpx + uvicorn.access at WARNING.

    httpx logs one INFO line per outbound request; at the poller's prod
    volume those would dominate both Cloud Logging and the Sentry logs
    budget, so they're floored to WARNING (a real failure already surfaces
    as a poll_*_failed ERROR). Restores the root logger afterwards so this
    doesn't perturb other tests' logging setup.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        setup_logging()
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("uvicorn.access").level == logging.WARNING
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)

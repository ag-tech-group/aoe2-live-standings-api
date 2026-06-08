"""Nudge-version table — the SSE refetch signal, pooler-compatible (#196 Option B).

Replaces Postgres ``LISTEN/NOTIFY`` for SSE nudges. ``LISTEN`` needs a
session-pinned connection, which Managed Connection Pooling (transaction mode)
can't provide — so instead the worker bumps a per-event ``polled_at`` here on
each commit, and every api instance polls this tiny table through its (pooled)
engine and fans a nudge out to its local SSE clients when an event's
``polled_at`` advances. No direct/session-pinned DB connection anywhere, so
``num_backends`` is fully decoupled from Cloud Run instance count.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class NudgeVersion(Base):
    """One row per SSE event type; ``polled_at`` is the version token.

    The worker UPSERTs ``polled_at = now()`` inside the same transaction as the
    polled data (so the signal commits iff the data does), and each api
    instance's poll loop publishes a nudge when ``polled_at`` advances past the
    value it last saw.
    """

    __tablename__ = "nudge_versions"

    # The EventType value: "standings", "live", or "matches".
    event: Mapped[str] = mapped_column(String, primary_key=True)

    # Bumped to now() on each emit; the api compares against its last-seen value.
    polled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

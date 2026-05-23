"""Leaderboard metadata model.

The polling worker upserts a row per leaderboard on startup from upstream
``getAvailableLeaderboards``; the ``/v1/leaderboards`` route reads here,
and the recent-matches poller reads ``matchtypes`` to build its
``matchtype_id -> leaderboard_id`` map. Splitting this metadata out of
in-process state is the first step toward separating the polling worker
from the read tier (issue #14).
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Leaderboard(Base):
    """One row per upstream leaderboard — id, display name, ranked flag, matchtypes."""

    __tablename__ = "leaderboards"

    # Upstream leaderboard id — matches ``Match.leaderboard_id`` and
    # ``PlayerRating.leaderboard_id``. Stored as the PK to mirror how
    # ``Player.profile_id`` and ``Match.match_id`` use upstream identifiers.
    leaderboard_id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    is_ranked: Mapped[bool]
    # Set of upstream matchtype IDs this leaderboard covers. Loaded by the
    # recent-matches poller to build its ``matchtype_id -> leaderboard_id``
    # map; not exposed on the read API.
    matchtypes: Mapped[list[int]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

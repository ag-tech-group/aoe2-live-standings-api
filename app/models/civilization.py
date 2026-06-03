"""Civilization reference model.

The polling worker upserts one row per civilization from the ``races``
array of upstream ``getAvailableLeaderboards`` (Relic calls civs "races").
The ``/v1/civilizations`` route reads here, and the tournament civ-stats /
recent-matchup endpoints fold the name onto each civ id. Same
source-of-truth-in-the-DB pattern as ``Leaderboard`` — the worker writes,
the read tier reads, no per-request upstream call.
"""

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Civilization(Base):
    """One AoE2 civilization — upstream ``civilization_id`` + display name."""

    __tablename__ = "civilizations"

    # Upstream civ id (Relic ``races[].id``) — matches
    # ``MatchPlayer.civilization_id``. Stored as the PK, mirroring how
    # ``Leaderboard.leaderboard_id`` uses the upstream identifier.
    civilization_id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

"""Tournament response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TournamentRead(BaseModel):
    """A tournament — a named roster of players tracked on one leaderboard.

    Configuration rather than polled data: a tournament's standings,
    matches, and live state are served under ``/v1/tournaments/{slug}/...``.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    leaderboard_id: int
    start_date: datetime | None
    end_date: datetime | None
    created_at: datetime

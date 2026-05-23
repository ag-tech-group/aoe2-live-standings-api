"""Schemas for the per-user `/v1/me` surface."""

from __future__ import annotations

from pydantic import BaseModel

from app.schemas.tournament import TournamentRead


class MeRead(BaseModel):
    """The authenticated user's identity + everything they can manage.

    One round-trip the frontend can hit on app load to answer "who am
    I and what can I admin?" — replaces probing per-tournament owner
    endpoints to derive the admin-UI map.
    """

    user_id: str
    owned_tournaments: list[TournamentRead]

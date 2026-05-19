"""Match + MatchPlayer response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.match import MatchOutcome, MatchState


class MatchPlayerRead(BaseModel):
    """One MatchPlayer row, embedded inside MatchRead.

    `outcome`, `old_rating`, `new_rating` are null while the match is in
    progress; the upstream fills them in on completion.
    """

    model_config = ConfigDict(from_attributes=True)

    profile_id: int
    civilization_id: int
    team_id: int
    outcome: MatchOutcome | None
    old_rating: int | None
    new_rating: int | None
    xp_gained: int


class MatchRead(BaseModel):
    """A match plus all of its MatchPlayer rows.

    Players are always included — even in list views — so a tournament UI
    can render a full match card (both sides, outcome, Elo delta) without a
    second round-trip. 1v1 ranked is two rows, team games top out at eight.
    """

    model_config = ConfigDict(from_attributes=True)

    match_id: int
    map_name: str
    matchtype_id: int
    leaderboard_id: int | None
    started_at: datetime
    completed_at: datetime | None
    description: str | None
    state: MatchState
    updated_at: datetime
    players: list[MatchPlayerRead]


class MatchDetail(MatchRead):
    """Single-match response (``GET /v1/matches/{match_id}``).

    Adds the ``last_polled_at`` envelope field on top of the entity shape.
    Single-resource endpoints stay flat — `last_polled_at` sits beside the
    entity's own fields rather than wrapping them — so the generated TS
    types don't add a layer of nesting.
    """

    last_polled_at: datetime | None = None

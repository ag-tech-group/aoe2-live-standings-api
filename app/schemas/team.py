"""Team standings response schemas."""

from __future__ import annotations

from pydantic import BaseModel


class TeamMemberRead(BaseModel):
    """One member of a team, with their current rating on the tournament's leaderboard."""

    profile_id: int
    alias: str
    current_rating: int


class TeamStandingRow(BaseModel):
    """One row in a tournament's team standings.

    ``combined_rating_sum`` is the sum of the members' current ratings on
    the tournament's leaderboard; ``combined_rating_average`` is that sum
    over the member count. Only members with a rating on that leaderboard
    are counted — a member the poller hasn't rated yet is omitted.
    """

    team_id: int
    name: str
    initials: str
    member_count: int
    combined_rating_sum: int
    combined_rating_average: float
    members: list[TeamMemberRead]

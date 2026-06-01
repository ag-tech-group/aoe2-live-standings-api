"""Team request/response schemas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TeamMemberRead(BaseModel):
    """One member of a team, with their current rating + live-match status.

    Shape parallels the per-player ``StandingRow`` fields the web app
    already renders on the standings tab: ``country`` for the flag pill,
    ``in_match`` / ``live_match_id`` for the live badge. Same source as
    ``StandingRow`` (see ``get_team_standings`` for the query), so a
    member's status here matches their standings row in the same poll.
    """

    profile_id: int
    alias: str
    # ISO 3166-1 alpha-2, lowercase — same shape as ``StandingRow.country``.
    # Nullable: upstream sometimes returns players without a country set.
    country: str | None
    current_rating: int
    # True while the member is in a live (staging / in-progress) match,
    # as of the last live poll (~15s cadence). ``live_match_id`` is that
    # match's id when ``in_match`` is true, else null.
    in_match: bool
    live_match_id: int | None
    # True when this member is the team's captain. At most one captain per
    # team; a team may also have no captain (all members ``false``).
    is_captain: bool


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


class TeamRead(BaseModel):
    """A team's stored fields — its identity and display strings.

    The plain team row, without the computed aggregates of
    ``TeamStandingRow``. Returned when a team is created or updated.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    tournament_id: int
    name: str
    initials: str


class TeamCreate(BaseModel):
    """Request body for creating a team within a tournament."""

    name: str = Field(min_length=1, max_length=200)
    initials: str = Field(min_length=1, max_length=8)


class TeamUpdate(BaseModel):
    """Partial update for a team (``PATCH``).

    Both fields are optional; only those present in the request body are
    applied. Both back non-nullable columns, so an explicit ``null`` is
    rejected with 422.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    initials: str | None = Field(default=None, min_length=1, max_length=8)

    @field_validator("name", "initials")
    @classmethod
    def _reject_explicit_null(cls, value: object) -> object:
        # See TournamentUpdate: a None here is an explicit null for a
        # non-nullable column, not an "unset" field.
        if value is None:
            raise ValueError("may not be null")
        return value


class TeamMemberCreate(BaseModel):
    """Request body for adding a profile to a team."""

    profile_id: int = Field(gt=0)


class TeamCaptainSet(BaseModel):
    """Request body for ``PATCH /teams/{team_id}/captain`` — designates the captain.

    The profile must already be a member of the team; the endpoint
    atomically clears any existing captain on the team and sets this one.
    """

    profile_id: int = Field(gt=0)

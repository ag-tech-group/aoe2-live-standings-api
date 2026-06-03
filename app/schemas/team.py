"""Team request/response schemas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from app.schemas.leaderboard import CivStat


class TeamMemberRead(BaseModel):
    """One member of a team, with their ratings + live-match status.

    Shape parallels the per-player ``StandingRow`` fields the web app
    already renders on the standings tab: ``country`` for the flag pill,
    ``in_match`` / ``live_match_id`` for the live badge. Same source as
    ``StandingRow`` (see ``get_team_standings`` for the query), so a
    member's status here matches their standings row in the same poll.
    """

    # The roster-row surrogate id — the management key for team-member
    # mutations (#167). Stable across an unlinked row's linking to a
    # polled identity.
    tournament_player_id: int
    # The polled-identity id. Null for an unlinked entrant whose
    # ``profile_id`` hasn't been linked yet — the roster row carries
    # only ``name`` until linked (see ``TournamentPlayer``).
    profile_id: int | None
    # The polled identity's display name. Null when the poller hasn't
    # picked the profile up yet (linked-but-not-polled member). For an
    # unlinked member, this falls back to the roster row's ``name``.
    alias: str | None
    # ISO 3166-1 alpha-2, lowercase — same shape as ``StandingRow.country``.
    # Null when upstream returns no country, or when ``Player`` hasn't
    # been polled yet (see ``alias``).
    country: str | None
    # Null when the member has no rating row on the tournament's
    # leaderboard yet (e.g. linked profile that hasn't played a ranked
    # game). The team-mgmt query left-joins ``PlayerRating`` so such a
    # member is listed under ``members`` rather than dropped (#166).
    current_rating: int | None
    # Lifetime peak on this leaderboard. Drives the team-standings
    # ``combined_rating_*`` aggregates (see ``TeamStandingRow``). Null
    # when the rating row has no recorded peak, or when the rating row
    # itself doesn't exist yet (see ``current_rating``).
    max_rating: int | None
    # True while the member is in a live (staging / in-progress) match,
    # as of the last live poll (~15s cadence). ``live_match_id`` is that
    # match's id when ``in_match`` is true, else null.
    in_match: bool
    live_match_id: int | None
    # True when this member is the team's captain. At most one captain per
    # team; a team may also have no captain (all members ``false``).
    is_captain: bool
    # In-tournament-window win/loss (completed matches on the tournament's
    # leaderboard within its date window) — the same figures as the
    # member's per-player ``tournament_record``. Both 0 for an unlinked or
    # not-yet-polled member, or one with no in-window games.
    wins: int
    losses: int


class TeamStandingRow(BaseModel):
    """One row in a tournament's team standings.

    ``combined_rating_sum`` is the sum of the members' peak (lifetime
    ``max_rating``) ratings on the tournament's leaderboard;
    ``combined_rating_average`` is that sum over the count of members
    with a non-null peak. Every ``team_members`` row is listed under
    ``members`` regardless of rating status — a linked-but-unrated
    member (no ``PlayerRating`` on the leaderboard yet) is included with
    null rating fields but excluded from the combined sum and the
    average's denominator (#166).

    ``combined_wins`` / ``combined_losses`` sum the members' in-window
    ``tournament_record`` win/loss; ``win_pct`` is over that combined total
    (server-computed, null when no in-window games). ``civs`` aggregates the
    members' civ picks/wins across the team (#220) — same per-civ shape and
    ordering as ``/civ-stats``.
    """

    team_id: int
    name: str
    initials: str
    member_count: int
    combined_rating_sum: int
    combined_rating_average: float
    combined_wins: int
    combined_losses: int
    civs: list[CivStat]
    members: list[TeamMemberRead]

    @computed_field
    @property
    def win_pct(self) -> float | None:
        """Win percentage (0–100, 1 dp) over the team's combined in-window games; null when none."""
        total = self.combined_wins + self.combined_losses
        if total == 0:
            return None
        return round(self.combined_wins / total * 100, 1)


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
    """Request body for adding a roster row to a team.

    Keys on the roster row's surrogate ``id`` (``tournament_player_id``)
    rather than the polled ``profile_id`` so an unlinked entrant — a
    roster row with no ``profile_id`` yet — can be teamed (#167).
    """

    tournament_player_id: int = Field(gt=0)


class TeamCaptainSet(BaseModel):
    """Request body for ``PATCH /teams/{team_id}/captain`` — designates the captain.

    The roster row must already be a member of the team; the endpoint
    atomically clears any existing captain on the team and sets this one.
    """

    tournament_player_id: int = Field(gt=0)

from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class MatchState(StrEnum):
    STAGING = "staging"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class MatchOutcome(StrEnum):
    WIN = "win"
    LOSS = "loss"


class Match(Base):
    __tablename__ = "matches"

    match_id: Mapped[int] = mapped_column(primary_key=True)
    map_name: Mapped[str]
    matchtype_id: Mapped[int]
    # Derived from `matchtype_id` via the static `getAvailableLeaderboards`
    # cache; null when the match isn't on a leaderboard (e.g. custom lobby
    # without a matched leaderboard mapping).
    leaderboard_id: Mapped[int | None]
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    description: Mapped[str | None]
    state: Mapped[MatchState] = mapped_column(
        Enum(
            MatchState,
            name="match_state",
            native_enum=False,
            # Store the StrEnum value (lowercase, matches JSON wire format) in
            # the DB rather than the member name (uppercase). Otherwise the
            # CHECK constraint values and the inserted value would diverge.
            values_callable=lambda enum: [e.value for e in enum],
        ),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    players: Mapped[list["MatchPlayer"]] = relationship(
        back_populates="match",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        # `started_at` for the recent-matches feed; `state` for the live feed.
        Index("ix_matches_started_at", "started_at"),
        Index("ix_matches_state", "state"),
    )


class MatchPlayer(Base):
    __tablename__ = "match_players"

    match_id: Mapped[int] = mapped_column(
        ForeignKey("matches.match_id", ondelete="CASCADE"),
        primary_key=True,
    )
    # No FK to `players` — opponents of tracked players appear here too and
    # are not required to be tracked themselves.
    profile_id: Mapped[int] = mapped_column(primary_key=True)
    civilization_id: Mapped[int]
    team_id: Mapped[int]
    outcome: Mapped[MatchOutcome | None] = mapped_column(
        Enum(
            MatchOutcome,
            name="match_outcome",
            native_enum=False,
            values_callable=lambda enum: [e.value for e in enum],
        ),
    )
    old_rating: Mapped[int | None]
    new_rating: Mapped[int | None]
    xp_gained: Mapped[int]

    match: Mapped[Match] = relationship(back_populates="players")

    __table_args__ = (
        # Find all matches for a given player profile.
        Index("ix_match_players_profile_id", "profile_id"),
    )

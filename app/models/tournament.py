from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Tournament(Base):
    """One tracked tournament — a named roster of players on one leaderboard.

    Supersedes the single-deployment ``TRACKED_PROFILE_IDS`` env var: one
    API instance serves many tournaments, each with its own roster
    (``TournamentPlayer``), competition window, and optional teams.
    ``slug`` is the URL-friendly key used in ``/v1/tournaments/{slug}/...``
    routes.
    """

    __tablename__ = "tournaments"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str]
    # The leaderboard whose ratings this tournament's standings track
    # (e.g. 3 for 1v1 RM Ranked). One ladder per tournament.
    leaderboard_id: Mapped[int]
    # The competition window. Nullable so a tournament can be created
    # before its schedule is fixed; tournament-scoped stats treat a null
    # bound as open-ended.
    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    tracked_players: Mapped[list["TournamentPlayer"]] = relationship(
        back_populates="tournament",
        cascade="all, delete-orphan",
    )


class TournamentPlayer(Base):
    """A profile tracked by a tournament — the poller's per-tournament roster.

    No FK to ``players``: a profile is added to a tournament as *input* to
    the poller, before any ``Player`` row exists for it. Mirrors the
    FK-free ``profile_id`` on ``MatchPlayer`` / ``LiveMatchPlayer``.
    """

    __tablename__ = "tournament_players"

    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"),
        primary_key=True,
    )
    profile_id: Mapped[int] = mapped_column(primary_key=True)

    tournament: Mapped[Tournament] = relationship(back_populates="tracked_players")

    __table_args__ = (
        # Find every tournament a profile belongs to.
        Index("ix_tournament_players_profile_id", "profile_id"),
    )

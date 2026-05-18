from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Player(Base):
    __tablename__ = "players"

    profile_id: Mapped[int] = mapped_column(primary_key=True)
    alias: Mapped[str]
    country: Mapped[str | None] = mapped_column(String(2))
    steam_id: Mapped[str | None]
    level: Mapped[int]
    xp: Mapped[int]
    region_id: Mapped[int]
    clan_name: Mapped[str | None]
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    ratings: Mapped[list["PlayerRating"]] = relationship(
        back_populates="player",
        cascade="all, delete-orphan",
    )


class PlayerRating(Base):
    __tablename__ = "player_ratings"

    profile_id: Mapped[int] = mapped_column(
        ForeignKey("players.profile_id", ondelete="CASCADE"),
        primary_key=True,
    )
    leaderboard_id: Mapped[int] = mapped_column(primary_key=True)

    current_rating: Mapped[int]
    max_rating: Mapped[int]
    wins: Mapped[int]
    losses: Mapped[int]
    # Positive = win streak; negative = loss streak.
    streak: Mapped[int]
    drops: Mapped[int]
    # `-1` from upstream means "unranked on this leaderboard" — stored as null.
    rank: Mapped[int | None]
    rank_total: Mapped[int | None]
    region_rank: Mapped[int | None]
    region_rank_total: Mapped[int | None]
    last_match_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    player: Mapped[Player] = relationship(back_populates="ratings")

    __table_args__ = (
        # Composite index for the standings endpoint:
        # `WHERE leaderboard_id = ? ORDER BY current_rating DESC`.
        # B-tree indexes are bidirectional, so no DESC on the index is needed.
        Index(
            "ix_player_ratings_leaderboard_current_rating",
            "leaderboard_id",
            "current_rating",
        ),
    )

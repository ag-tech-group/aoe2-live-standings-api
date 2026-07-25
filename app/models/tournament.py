from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class RankBy(StrEnum):
    """Per-tournament ranking metric for standings and team aggregates (#290).

    ``PEAK_RATING`` (the default, and the launch event's format): positions
    rank on lifetime ``max_rating`` — carried in, immutable bar a new
    all-time high — and team ``combined_rating_*`` sums member peaks.
    ``CURRENT_RATING``: positions rank on live ``current_rating`` and team
    sums follow. Both ratings are always *returned* on every row; this
    only selects which one is the rank key and which one freezes at
    ``end_date``. The standings-history sweep supports ``PEAK_RATING``
    only for now (it 501s otherwise — its ratchet/baseline machinery is
    peak-specific).
    """

    PEAK_RATING = "peak_rating"
    CURRENT_RATING = "current_rating"


class Tournament(Base):
    """One tracked tournament — a named roster of players on one leaderboard.

    One API instance serves many tournaments, each with its own roster
    (``TournamentPlayer``), competition window, and optional teams.
    Tournaments are created exclusively via the management API
    (``POST /v1/tournaments``); a fresh deploy serves no data until an
    operator creates one. ``slug`` is the URL-friendly key used in
    ``/v1/tournaments/{slug}/...`` routes.
    """

    __tablename__ = "tournaments"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str]
    # The leaderboard whose ratings this tournament's standings track
    # (e.g. 3 for 1v1 RM Ranked). One ladder per tournament.
    leaderboard_id: Mapped[int]
    # Which rating metric positions rank on (#290) — see ``RankBy``.
    # server_default keeps every pre-existing row on the launch behavior.
    rank_by: Mapped[RankBy] = mapped_column(
        Enum(
            RankBy,
            name="rank_by",
            native_enum=False,
            # Store the StrEnum value (lowercase, matches the JSON wire
            # format), mirroring MatchState/MatchOutcome storage.
            values_callable=lambda enum: [e.value for e in enum],
        ),
        default=RankBy.PEAK_RATING,
        server_default=RankBy.PEAK_RATING.value,
    )
    # The competition window — the bounds for "what matches count for
    # this tournament" filters. Nullable so a tournament can be created
    # before its schedule is fixed; tournament-scoped queries treat a
    # null bound as open-ended.
    #
    # `end_date` is the END OF THE RATED WINDOW, not the event's last day
    # (renamed from `grand_finals_date`, reversing #76: The King's
    # Gauntlet's playoffs proved the two concepts diverge — the rated
    # window closes at the race end, days before the actual grand finals,
    # whose date is display schedule and belongs in the `presentation`
    # bag). Do NOT move this bound to a finals/closing-ceremony date —
    # that silently pours post-race ladder games into the record and
    # peaks. The legacy `grand_finals_date` column may still exist in the
    # DB (deliberately unmapped here — no code reads or writes it) until
    # the drop migration lands; see the rename's contract notes.
    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Tournament's prize pool in **minor currency units** (e.g. cents) — a
    # mutable owner-edited amount the consumer renders. Integer to avoid
    # float money bugs. No currency code: which currency it's denominated
    # in is fixed per event and stays in the consumer's per-tournament
    # config, keeping this API currency-agnostic.
    prize_pool_cents: Mapped[int | None] = mapped_column(Integer)
    # URLs of the tournament host's broadcast channels (Twitch / YouTube).
    # The frontend renders the host promo card from its own build config;
    # this list exists server-side only so the broadcast-live pollers can
    # resolve `host_stream_live` (#149). Typical: one Twitch + one YouTube.
    # An empty list (the default) means host-live detection is off and
    # `host_stream_live` always reports false.
    host_stream_urls: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Opaque tournament-level presentation data — phase schedule, bracket
    # state, showmatch billing, announcement copy, whatever the frontend
    # defines — set by an owner via PATCH and rendered by the consumer.
    # The API stores it but never interprets it (a pass-through bag), so
    # new display fields never need a migration. The tournament-level
    # mirror of ``TournamentPlayer.presentation``: it's what lets an event
    # transform for its post-window phases (playoffs, grand finals)
    # without tournament-format concepts leaking into the API contract.
    presentation: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    tracked_players: Mapped[list["TournamentPlayer"]] = relationship(
        back_populates="tournament",
        cascade="all, delete-orphan",
    )
    teams: Mapped[list["Team"]] = relationship(
        back_populates="tournament",
        cascade="all, delete-orphan",
    )
    owners: Mapped[list["TournamentOwner"]] = relationship(
        back_populates="tournament",
        cascade="all, delete-orphan",
    )


class TournamentPlayer(Base):
    """A roster entry — one first-class tournament player (#187).

    Every row has a ``name`` (the display label) and MAY also carry a
    ``profile_id`` linking it to a polled ``Player`` (ratings, country,
    matches, live status). An unlinked row — a ``name`` with no
    ``profile_id`` yet, typically a streamer whose account hasn't minted —
    is fully first-class: addressable and teamable by its surrogate ``id``,
    just without polled enrichment until it's linked. The surrogate ``id``
    PK is the identity everything addresses (#167); ``profile_id`` is an
    optional enrichment link, not an alternate identity (#187 dropped the
    old ``profile_id`` XOR ``name`` two-class split).

    ``name`` is NOT NULL (#187 Phase 3): every roster row has a display
    label. The application sets it on create; the poller never writes this
    table.

    No FK to ``players``: a profile is added as *input* to the poller,
    before any ``Player`` row exists for it; an unlinked row has no
    ``Player`` at all. Mirrors the FK-free ``profile_id`` on
    ``MatchPlayer`` / ``LiveMatchPlayer``.
    """

    __tablename__ = "tournament_players"

    id: Mapped[int] = mapped_column(primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"),
    )
    profile_id: Mapped[int | None] = mapped_column(nullable=True)
    name: Mapped[str] = mapped_column(String(64))
    # Opaque per-player presentation data for this tournament — stream
    # links, bio text, etc. — set by an owner via PATCH and rendered by the
    # consumer. The API stores it but never interprets it (a pass-through
    # bag), so new display fields never need a migration. Organizer-curated:
    # the poller never writes this table, so it survives every poll cycle.
    presentation: Mapped[dict] = mapped_column(JSON, default=dict)

    tournament: Mapped[Tournament] = relationship(back_populates="tracked_players")
    # Cascade so deleting a roster row clears its team memberships;
    # mirrors the DB-side ON DELETE CASCADE on team_members.
    team_memberships: Mapped[list["TeamMember"]] = relationship(
        back_populates="tournament_player",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        # profile_id is unique within a tournament when set; Postgres treats
        # NULLs as distinct in UNIQUE, so any number of unlinked rows (NULL
        # profile_id) coexist. A profile links to at most one roster row per
        # tournament.
        UniqueConstraint(
            "tournament_id",
            "profile_id",
            name="uq_tournament_players_tournament_id_profile_id",
        ),
        # name is unique within a tournament — display labels don't collide.
        UniqueConstraint(
            "tournament_id",
            "name",
            name="uq_tournament_players_tournament_id_name",
        ),
        # Find every tournament a profile belongs to.
        Index("ix_tournament_players_profile_id", "profile_id"),
    )


class TournamentCreator(Base):
    """A criticalbit user approved to *create* tournaments (#296).

    The operator-managed allowlist gating ``POST /v1/tournaments``:
    creation is invite-only (an organizer contacts the platform operator,
    who approves), while everything else about the auth model is
    unchanged — per-tournament management authorization stays
    ``TournamentOwner``, and approved creators still become the first
    owner of what they create.

    Same shape rationale as ``TournamentOwner``: no FK to any user table
    (identity lives in criticalbit-auth-api; ``user_id`` is the opaque
    UUID from the access token's ``sub`` claim), and rows are inserted
    out-of-band by the operator — a grant API would need a platform-admin
    concept this service deliberately doesn't have.
    """

    __tablename__ = "tournament_creators"

    # The criticalbit user UUID, exactly as it appears in the token `sub`.
    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class TournamentOwner(Base):
    """A criticalbit user authorized to manage a tournament.

    The authorization layer this service owns itself. Authentication
    (criticalbit-auth-api) only answers *who* a request is; this table
    answers *what they may edit* — a row grants its ``user_id`` write
    access to one tournament's metadata, roster, and teams.

    No FK to any user table: identity lives in criticalbit-auth-api, so
    ``user_id`` is the opaque UUID from the access token's ``sub`` claim.
    Rows are inserted out-of-band for now; an API to manage them is
    deferred to a follow-up issue.
    """

    __tablename__ = "tournament_owners"

    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # The criticalbit user UUID, exactly as it appears in the token `sub`.
    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    tournament: Mapped[Tournament] = relationship(back_populates="owners")

    __table_args__ = (
        # Find every tournament a user owns.
        Index("ix_tournament_owners_user_id", "user_id"),
    )


class Team(Base):
    """A team within a tournament — a named subset of its roster.

    A tournament may have any number of teams, or none (1v1 events have
    none). Teams are tournament-scoped: deleting a tournament cascades to
    its teams.
    """

    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"),
    )
    name: Mapped[str]
    # Short display code shown where the individual list shows a country.
    initials: Mapped[str] = mapped_column(String(8))

    tournament: Mapped[Tournament] = relationship(back_populates="teams")
    members: Mapped[list["TeamMember"]] = relationship(
        back_populates="team",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        # Find every team in a tournament.
        Index("ix_teams_tournament_id", "tournament_id"),
    )


class TeamMember(Base):
    """A roster row's membership in a team.

    Keys on ``tournament_player_id`` rather than ``profile_id`` so
    unlinked entrants (a roster row with no ``profile_id`` yet) can be
    teamed — the original #167 ask. The FK cascades on
    roster-row delete, so removing a player from the tournament also
    removes their team memberships.
    """

    __tablename__ = "team_members"

    team_id: Mapped[int] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tournament_player_id: Mapped[int] = mapped_column(
        ForeignKey("tournament_players.id", ondelete="CASCADE"),
        primary_key=True,
    )
    is_captain: Mapped[bool] = mapped_column(server_default=false())

    team: Mapped[Team] = relationship(back_populates="members")
    tournament_player: Mapped[TournamentPlayer] = relationship(back_populates="team_memberships")

    __table_args__ = (
        Index("ix_team_members_tournament_player_id", "tournament_player_id"),
        # At most one captain per team — partial unique index on team_id
        # filtered to ``is_captain``. The endpoint also clears any current
        # captain before setting a new one, so the app-level write path is
        # correct even without the DB constraint; the index is
        # belt-and-suspenders for the prod path and catches buggy callers.
        # SQLite (test path via ``metadata.create_all``) needs its own
        # ``sqlite_where`` — without it, SQLAlchemy emits a plain UNIQUE
        # INDEX on ``team_id`` and rejects every team with 2+ members.
        Index(
            "uq_team_members_captain",
            "team_id",
            unique=True,
            postgresql_where=text("is_captain"),
            sqlite_where=text("is_captain"),
        ),
    )

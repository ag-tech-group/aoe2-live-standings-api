from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class HostLiveStream(Base):
    """A tournament host's channel broadcasting live right now, per platform.

    Sibling of ``LiveStream`` but keyed on ``tournament_id`` because the
    host isn't a roster row — they're tournament metadata (#149). Same
    snapshot semantics: the broadcast-live pollers fully rewrite their
    platform's rows each cycle, and a row's presence means the host's
    configured channel for that platform was live as of the last poll.
    Backs the ``host_stream_live`` flag on ``TournamentRead``.
    """

    __tablename__ = "host_live_streams"

    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # "twitch" | "youtube" (see app.poller.broadcast platform constants).
    platform: Mapped[str] = mapped_column(primary_key=True)


class LiveStream(Base):
    """A roster row whose channel is broadcasting live right now, per platform.

    A transient snapshot the broadcast-live pollers fully rewrite each
    cycle, mirroring ``LiveMatchPlayer``: a row's mere presence means the
    roster entry was live on that platform as of the last poll. Partitioned
    by ``platform`` (part of the PK) so the fast Twitch poller and the slow,
    quota-bound YouTube poller each replace only their own rows without
    clobbering the other's — and a player live on both platforms gets a row
    for each. Backs the ``stream_live`` flag on the standings row.

    Keyed on ``tournament_player_id`` rather than ``profile_id`` so an
    unlinked roster row (``profile_id IS NULL``, allowed since the unify
    migration) can still be reported as live: every roster entry has a
    stable surrogate id, linked or not. The FK cascades on delete so
    removing a roster row tears down any live-stream snapshot for it.
    """

    __tablename__ = "live_streams"

    # tournament_player_id leads the composite PK, so the standings endpoint's
    # `WHERE tournament_player_id IN (...)` lookup rides the PK index — no
    # separate index needed (unlike LiveMatchPlayer, where profile_id trails).
    tournament_player_id: Mapped[int] = mapped_column(
        ForeignKey("tournament_players.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # "twitch" | "youtube" (see app.poller.broadcast platform constants).
    platform: Mapped[str] = mapped_column(primary_key=True)

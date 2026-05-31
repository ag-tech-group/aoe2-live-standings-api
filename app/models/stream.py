from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LiveStream(Base):
    """A roster row whose channel is broadcasting live right now, per platform.

    A transient snapshot the broadcast-live pollers fully rewrite each
    cycle, mirroring ``LiveMatchPlayer``: a row's mere presence means the
    roster entry was live on that platform as of the last poll. Partitioned
    by ``platform`` (part of the PK) so the fast Twitch poller and the slow,
    quota-bound YouTube poller each replace only their own rows without
    clobbering the other's — and a player live on both platforms gets a row
    for each. Backs the ``stream_live`` flag on the standings row.

    Keyed on ``tournament_player_id`` rather than ``profile_id`` so a
    placeholder roster row (``profile_id IS NULL``, set since the unify
    migration) can still be reported as live: every roster entry has a
    stable surrogate id, polled or not. The FK cascades on delete so
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

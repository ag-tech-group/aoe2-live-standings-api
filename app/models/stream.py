from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LiveStream(Base):
    """A roster profile whose channel is broadcasting live right now, per platform.

    A transient snapshot the broadcast-live pollers fully rewrite each
    cycle, mirroring ``LiveMatchPlayer``: a row's mere presence means the
    player was live on that platform as of the last poll. Partitioned by
    ``platform`` (part of the PK) so the fast Twitch poller and the slow,
    quota-bound YouTube poller each replace only their own rows without
    clobbering the other's — and a player live on both platforms gets a row
    for each. Backs the ``stream_live`` flag on the standings row.

    No FK to ``players``: a rostered profile can have a stream link before
    ``poll_player_stats`` has written its Player row. Mirrors LiveMatchPlayer.
    """

    __tablename__ = "live_streams"

    # profile_id leads the composite PK, so the standings endpoint's
    # `WHERE profile_id IN (...)` lookup rides the PK index — no separate
    # index needed (unlike LiveMatchPlayer, where profile_id trails).
    profile_id: Mapped[int] = mapped_column(primary_key=True)
    # "twitch" | "youtube" (see app.poller.broadcast platform constants).
    platform: Mapped[str] = mapped_column(primary_key=True)

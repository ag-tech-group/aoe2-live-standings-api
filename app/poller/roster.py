"""Resolving which profiles the poller tracks — the union of tournament rosters.

The tracked-player set is no longer a static env var; it is the union of
every tournament's ``TournamentPlayer`` roster. ``ensure_seed_tournament``
bootstraps a first tournament from env config when the database has none,
so a fresh deploy — and the migration from the old single-tournament
setup — needs no manual seeding.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Tournament, TournamentPlayer
from app.poller.broadcast import extract_stream_urls

logger = structlog.get_logger(__name__)


async def ensure_seed_tournament(session: AsyncSession) -> None:
    """Create a seed tournament from env config if the database has none.

    Idempotent: a no-op once any tournament exists. Skips silently when
    there is no roster to seed (``tracked_profile_ids`` empty).
    """
    existing = (await session.execute(select(Tournament.id).limit(1))).first()
    if existing is not None:
        return

    profile_ids = settings.tracked_profile_id_list
    if not profile_ids:
        logger.info("seed_tournament_skipped", reason="no tracked_profile_ids")
        return

    tournament = Tournament(
        slug=settings.tournament_slug,
        name=settings.tournament_name,
        leaderboard_id=settings.tournament_leaderboard_id,
        start_date=settings.tournament_start_date,
        grand_finals_date=settings.tournament_grand_finals_date,
    )
    tournament.tracked_players = [TournamentPlayer(profile_id=pid) for pid in profile_ids]
    session.add(tournament)
    await session.commit()
    logger.info("seed_tournament_created", slug=tournament.slug, players=len(profile_ids))


async def get_tracked_profile_ids(session: AsyncSession) -> list[int]:
    """Return the union of every tournament's tracked profile IDs."""
    rows = await session.execute(select(TournamentPlayer.profile_id).distinct())
    return list(rows.scalars().all())


async def get_stream_urls_by_profile(session: AsyncSession) -> dict[int, list[str]]:
    """Map each tracked profile to the stream URLs in its presentation bag(s).

    A profile can sit on several tournament rosters; its URLs are the union
    across them (deduped, order-preserving). Profiles with no stream URLs
    are omitted. Feeds the broadcast-live pollers.
    """
    rows = await session.execute(select(TournamentPlayer.profile_id, TournamentPlayer.presentation))
    by_profile: dict[int, list[str]] = {}
    for profile_id, presentation in rows.all():
        for url in extract_stream_urls(presentation or {}):
            bucket = by_profile.setdefault(profile_id, [])
            if url not in bucket:
                bucket.append(url)
    return by_profile

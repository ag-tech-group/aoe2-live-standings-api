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
    """Return the union of every tournament's tracked profile IDs.

    Placeholder roster rows (``profile_id IS NULL``) are skipped — by
    design they're announced-but-unjoined entrants the poller can't
    fetch anything for. They appear once the host promotes them.
    """
    rows = await session.execute(
        select(TournamentPlayer.profile_id)
        .where(TournamentPlayer.profile_id.is_not(None))
        .distinct()
    )
    return list(rows.scalars().all())


async def get_host_stream_urls_by_tournament(session: AsyncSession) -> dict[int, list[str]]:
    """Map each tournament's ``id`` to its host channel URLs.

    Feeds the broadcast-live pollers' host detection (#149). Tournaments
    with an empty ``host_stream_urls`` list are omitted, so detection is
    off by default and only the tournaments whose hosts have URLs
    configured cost any Helix quota. URLs are deduped, order-preserving.
    """
    rows = await session.execute(select(Tournament.id, Tournament.host_stream_urls))
    by_tournament: dict[int, list[str]] = {}
    for tournament_id, urls in rows.all():
        if not urls:
            continue
        bucket: list[str] = []
        for url in urls:
            if url not in bucket:
                bucket.append(url)
        by_tournament[tournament_id] = bucket
    return by_tournament


async def get_stream_urls_by_roster_row(session: AsyncSession) -> dict[int, list[str]]:
    """Map each roster row's ``TournamentPlayer.id`` to its stream URLs.

    Both polled and placeholder rows are included — broadcast-live detection
    only needs a stable identity per roster entry, and the surrogate ``id``
    PK gives us one even when ``profile_id`` is null (#147). Rows with no
    stream URLs are omitted. A single polled profile that sits on several
    tournament rosters appears once per row here; per-row keying keeps each
    tournament's snapshot independent. URLs are deduped, order-preserving.
    """
    rows = await session.execute(select(TournamentPlayer.id, TournamentPlayer.presentation))
    by_row: dict[int, list[str]] = {}
    for row_id, presentation in rows.all():
        for url in extract_stream_urls(presentation or {}):
            bucket = by_row.setdefault(row_id, [])
            if url not in bucket:
                bucket.append(url)
    return by_row

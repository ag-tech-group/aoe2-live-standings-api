"""Resolving which profiles the poller tracks — the union of tournament rosters.

The tracked-player set is the union of every tournament's
``TournamentPlayer`` roster. Tournaments are created exclusively via the
management API (``POST /v1/tournaments``); there is no implicit seed
bootstrap. A fresh deploy serves no data until an operator creates a
tournament.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Tournament, TournamentPlayer
from app.poller.broadcast import extract_stream_urls


async def get_tracked_profile_ids(session: AsyncSession) -> list[int]:
    """Return the union of every tournament's linked profile IDs.

    Unlinked roster rows (``profile_id IS NULL``) are skipped — only a row
    with a profile link is pollable; there's nothing to fetch for an entry
    whose account hasn't minted yet. It starts being polled once it's
    linked (a PATCH sets its ``profile_id``).
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

    Both linked and unlinked rows are included — broadcast-live detection
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

"""Broadcast-live pollers (#112): maintain the ``live_streams`` snapshot.

Two tasks share the table, partitioned by platform:

- ``poll_twitch_live`` (fast, ~60s) — one batched Helix call covers the roster.
- ``poll_youtube_live`` (slow, ~30m) — best-effort and quota-bound; only
  checks players who have a YouTube link but *no* Twitch link (Twitch wins),
  keeping the expensive ``search.list`` calls down to a handful of channels.

Each tick computes the live roster-row set + the live host-tournament set
for its platform and only rewrites + nudges when *either* changed since
last cycle — stream status flips rarely, so there's no point nudging
every tick. Roster + host channels share one platform query per cycle
(one Helix call, one YouTube batch) so host detection is essentially
free on top of the roster poll (#149).

Keyed on ``TournamentPlayer.id`` for the roster (placeholders included,
#147) and ``Tournament.id`` for the host channels.
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.events import EventType, emit_nudge
from app.models import HostLiveStream, LiveStream
from app.poller.broadcast import (
    PLATFORM_TWITCH,
    PLATFORM_YOUTUBE,
    TwitchLiveClient,
    YouTubeLiveClient,
    parse_twitch_login,
    parse_youtube_ref,
)
from app.poller.roster import (
    get_host_stream_urls_by_tournament,
    get_stream_urls_by_roster_row,
)
from app.poller.upserts import replace_host_live_streams, replace_live_streams

logger = structlog.get_logger(__name__)

_TWITCH_INTERVAL_SECONDS = 60
# 30 min: with the free 10k-units/day quota and search.list at 100 units,
# this stays in budget for a couple of channels. The "no Twitch link" filter
# keeps the set tiny; raise this (or request more quota) before adding more
# YouTube-only players.
_YOUTUBE_INTERVAL_SECONDS = 1800


async def _current_live_roster_rows(session: AsyncSession, platform: str) -> set[int]:
    rows = await session.execute(
        select(LiveStream.tournament_player_id).where(LiveStream.platform == platform)
    )
    return set(rows.scalars().all())


async def _current_live_host_tournaments(session: AsyncSession, platform: str) -> set[int]:
    rows = await session.execute(
        select(HostLiveStream.tournament_id).where(HostLiveStream.platform == platform)
    )
    return set(rows.scalars().all())


async def _apply_live_sets(
    session_maker: async_sessionmaker,
    platform: str,
    live_rows: set[int],
    live_host_tournaments: set[int],
) -> bool:
    """Rewrite ``platform``'s rows in both snapshots + nudge, if either changed.

    Reads current state first so a steady stream costs one cheap query per
    snapshot and no nudge. Apply is atomic — both snapshots and the nudge
    commit together — so an SSE subscriber never sees one half updated.
    Returns whether anything changed.
    """
    async with session_maker() as session:
        current_rows = await _current_live_roster_rows(session, platform)
        current_hosts = await _current_live_host_tournaments(session, platform)
        if current_rows == live_rows and current_hosts == live_host_tournaments:
            return False
        await replace_live_streams(session, platform, sorted(live_rows))
        await replace_host_live_streams(session, platform, sorted(live_host_tournaments))
        # stream_live / host_stream_live ride live-data resources — nudge so
        # SSE subscribers refetch standings and the tournament record.
        await emit_nudge(session, EventType.LIVE)
        await session.commit()
    return True


def _twitch_logins_by_key(stream_urls: dict[int, list[str]]) -> dict[str, set[int]]:
    """Invert key->URLs into twitch_login->keys for the live lookup.

    ``key`` is opaque to this helper — it's a roster-row id for the player
    poll and a tournament id for the host poll. The shape is identical
    either way, so one helper covers both.
    """
    by_login: dict[str, set[int]] = {}
    for key, urls in stream_urls.items():
        for url in urls:
            login = parse_twitch_login(url)
            if login:
                by_login.setdefault(login, set()).add(key)
    return by_login


def _youtube_refs_by_key(
    stream_urls: dict[int, list[str]],
) -> dict[tuple[str, str], set[int]]:
    """YouTube channel ref -> keys, skipping anyone with a Twitch link.

    Twitch detection is cheap and preferred, so a channel reachable on
    Twitch never costs YouTube quota — only Twitch-less keys are checked
    here. ``key`` is opaque (roster-row id or tournament id).
    """
    by_ref: dict[tuple[str, str], set[int]] = {}
    for key, urls in stream_urls.items():
        if any(parse_twitch_login(url) for url in urls):
            continue
        for url in urls:
            ref = parse_youtube_ref(url)
            if ref:
                by_ref.setdefault(ref, set()).add(key)
    return by_ref


async def tick_twitch_live(
    twitch: TwitchLiveClient,
    stream_urls: dict[int, list[str]],
    host_urls: dict[int, list[str]],
    session_maker: async_sessionmaker,
) -> None:
    """One Twitch cycle: resolve logins for roster + hosts in one Helix call.

    Emits ``poll_twitch_live_ok`` per tick (not just on change) so a
    Cloud Monitoring ``condition_absent`` alert can detect a wedged
    poller — matches the per-cycle heartbeat the other pollers emit.
    """
    rows_by_login = _twitch_logins_by_key(stream_urls)
    hosts_by_login = _twitch_logins_by_key(host_urls)
    all_logins = set(rows_by_login) | set(hosts_by_login)
    live_logins = await twitch.get_live_logins(list(all_logins)) if all_logins else set()
    live_rows = {row_id for login in live_logins for row_id in rows_by_login.get(login, set())}
    live_hosts = {tid for login in live_logins for tid in hosts_by_login.get(login, set())}
    await _apply_live_sets(session_maker, PLATFORM_TWITCH, live_rows, live_hosts)
    logger.info("poll_twitch_live_ok", live=len(live_rows), live_hosts=len(live_hosts))


async def tick_youtube_live(
    youtube: YouTubeLiveClient,
    stream_urls: dict[int, list[str]],
    host_urls: dict[int, list[str]],
    session_maker: async_sessionmaker,
) -> None:
    """One YouTube cycle: check Twitch-less roster + host channels, persist + nudge on change.

    Emits ``poll_youtube_live_ok`` per tick (not just on change) so a
    Cloud Monitoring ``condition_absent`` alert can detect a wedged
    poller — matches the per-cycle heartbeat the other pollers emit.
    """
    rows_by_ref = _youtube_refs_by_key(stream_urls)
    hosts_by_ref = _youtube_refs_by_key(host_urls)
    all_refs = set(rows_by_ref) | set(hosts_by_ref)
    live_refs = await youtube.get_live_refs(list(all_refs)) if all_refs else set()
    live_rows = {row_id for ref in live_refs for row_id in rows_by_ref.get(ref, set())}
    live_hosts = {tid for ref in live_refs for tid in hosts_by_ref.get(ref, set())}
    await _apply_live_sets(session_maker, PLATFORM_YOUTUBE, live_rows, live_hosts)
    logger.info("poll_youtube_live_ok", live=len(live_rows), live_hosts=len(live_hosts))


async def run_twitch_live_poller(
    twitch: TwitchLiveClient,
    session_maker: async_sessionmaker,
    interval_seconds: int = _TWITCH_INTERVAL_SECONDS,
) -> None:
    """Long-running Twitch poller. Re-resolves the roster + host URLs each cycle."""
    while True:
        try:
            async with session_maker() as session:
                stream_urls = await get_stream_urls_by_roster_row(session)
                host_urls = await get_host_stream_urls_by_tournament(session)
            await tick_twitch_live(twitch, stream_urls, host_urls, session_maker)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("poll_twitch_live_failed", error=str(e))
        await asyncio.sleep(interval_seconds)


async def run_youtube_live_poller(
    youtube: YouTubeLiveClient,
    session_maker: async_sessionmaker,
    interval_seconds: int = _YOUTUBE_INTERVAL_SECONDS,
) -> None:
    """Long-running YouTube poller. Slow cadence — see the quota note above."""
    while True:
        try:
            async with session_maker() as session:
                stream_urls = await get_stream_urls_by_roster_row(session)
                host_urls = await get_host_stream_urls_by_tournament(session)
            await tick_youtube_live(youtube, stream_urls, host_urls, session_maker)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("poll_youtube_live_failed", error=str(e))
        await asyncio.sleep(interval_seconds)

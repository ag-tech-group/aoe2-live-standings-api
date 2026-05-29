"""Broadcast-live pollers (#112): maintain the ``live_streams`` snapshot.

Two tasks share the table, partitioned by platform:

- ``poll_twitch_live`` (fast, ~60s) — one batched Helix call covers the roster.
- ``poll_youtube_live`` (slow, ~30m) — best-effort and quota-bound; only
  checks players who have a YouTube link but *no* Twitch link (Twitch wins),
  keeping the expensive ``search.list`` calls down to a handful of channels.

Each tick computes the live profile set for its platform and only rewrites
its rows + emits a nudge when that set changed since the last cycle — stream
status flips rarely, so there's no point nudging (and refetching) every tick.
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.events import EventType, emit_nudge
from app.models import LiveStream
from app.poller.broadcast import (
    PLATFORM_TWITCH,
    PLATFORM_YOUTUBE,
    TwitchLiveClient,
    YouTubeLiveClient,
    parse_twitch_login,
    parse_youtube_ref,
)
from app.poller.roster import get_stream_urls_by_profile
from app.poller.upserts import replace_live_streams

logger = structlog.get_logger(__name__)

_TWITCH_INTERVAL_SECONDS = 60
# 30 min: with the free 10k-units/day quota and search.list at 100 units,
# this stays in budget for a couple of channels. The "no Twitch link" filter
# keeps the set tiny; raise this (or request more quota) before adding more
# YouTube-only players.
_YOUTUBE_INTERVAL_SECONDS = 1800


async def _current_live_profiles(session: AsyncSession, platform: str) -> set[int]:
    rows = await session.execute(
        select(LiveStream.profile_id).where(LiveStream.platform == platform)
    )
    return set(rows.scalars().all())


async def _apply_live_set(
    session_maker: async_sessionmaker, platform: str, live_profiles: set[int]
) -> bool:
    """Rewrite ``platform``'s rows + nudge, but only if the set changed.

    Returns whether anything changed. Reading the current set first means a
    steady stream state costs one cheap query and no nudge.
    """
    async with session_maker() as session:
        if await _current_live_profiles(session, platform) == live_profiles:
            return False
        await replace_live_streams(session, platform, sorted(live_profiles))
        # stream_live rides the standings row — nudge so SSE subscribers refetch.
        await emit_nudge(session, EventType.LIVE)
        await session.commit()
    return True


def _twitch_logins_by_profile(stream_urls: dict[int, list[str]]) -> dict[str, set[int]]:
    """Invert profile->URLs into twitch_login->profiles for the live lookup."""
    by_login: dict[str, set[int]] = {}
    for profile_id, urls in stream_urls.items():
        for url in urls:
            login = parse_twitch_login(url)
            if login:
                by_login.setdefault(login, set()).add(profile_id)
    return by_login


def _youtube_refs_by_profile(
    stream_urls: dict[int, list[str]],
) -> dict[tuple[str, str], set[int]]:
    """YouTube channel ref -> profiles, skipping anyone with a Twitch link.

    Twitch detection is cheap and preferred, so a player reachable on Twitch
    never costs YouTube quota — only Twitch-less players are checked here.
    """
    by_ref: dict[tuple[str, str], set[int]] = {}
    for profile_id, urls in stream_urls.items():
        if any(parse_twitch_login(url) for url in urls):
            continue
        for url in urls:
            ref = parse_youtube_ref(url)
            if ref:
                by_ref.setdefault(ref, set()).add(profile_id)
    return by_ref


async def tick_twitch_live(
    twitch: TwitchLiveClient,
    stream_urls: dict[int, list[str]],
    session_maker: async_sessionmaker,
) -> None:
    """One Twitch cycle: resolve live logins, fold to profiles, persist on change."""
    by_login = _twitch_logins_by_profile(stream_urls)
    live_logins = await twitch.get_live_logins(list(by_login)) if by_login else set()
    live_profiles = {pid for login in live_logins for pid in by_login[login]}
    if await _apply_live_set(session_maker, PLATFORM_TWITCH, live_profiles):
        logger.info("poll_twitch_live_ok", live=len(live_profiles))


async def tick_youtube_live(
    youtube: YouTubeLiveClient,
    stream_urls: dict[int, list[str]],
    session_maker: async_sessionmaker,
) -> None:
    """One YouTube cycle: check Twitch-less channels, persist on change."""
    by_ref = _youtube_refs_by_profile(stream_urls)
    live_refs = await youtube.get_live_refs(list(by_ref)) if by_ref else set()
    live_profiles = {pid for ref in live_refs for pid in by_ref[ref]}
    if await _apply_live_set(session_maker, PLATFORM_YOUTUBE, live_profiles):
        logger.info("poll_youtube_live_ok", live=len(live_profiles))


async def run_twitch_live_poller(
    twitch: TwitchLiveClient,
    session_maker: async_sessionmaker,
    interval_seconds: int = _TWITCH_INTERVAL_SECONDS,
) -> None:
    """Long-running Twitch poller. Re-resolves the roster each cycle."""
    while True:
        try:
            async with session_maker() as session:
                stream_urls = await get_stream_urls_by_profile(session)
            await tick_twitch_live(twitch, stream_urls, session_maker)
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
                stream_urls = await get_stream_urls_by_profile(session)
            await tick_youtube_live(youtube, stream_urls, session_maker)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("poll_youtube_live_failed", error=str(e))
        await asyncio.sleep(interval_seconds)

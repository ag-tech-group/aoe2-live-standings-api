"""Twitch + YouTube broadcast-live detection clients and URL parsing (#112).

Detects whether a roster player's channel is *streaming right now* — a
different signal from ``in_match`` (the player is in a tournament game). A
player can be live without being in a tracked match, or vice-versa.

Twitch is the primary source: one app access token plus a single batched
Helix ``/streams`` call covers the whole roster cheaply. YouTube is a
best-effort fallback for players with no Twitch link — the Data API's free
quota (10k units/day, 100 per ``search.list`` live check, no batch form)
only stretches to a handful of channels on a slow cadence.

Stream links come from each roster player's opaque ``presentation`` bag.
This is the one place the API interprets that bag, by necessity: the
poller reads any twitch.tv / youtube.com URL it finds there (e.g. the
frontend's ``streamUrls`` list).
"""

from __future__ import annotations

import time
from typing import NamedTuple
from urllib.parse import urlparse

import httpx
import structlog

logger = structlog.get_logger(__name__)

PLATFORM_TWITCH = "twitch"
PLATFORM_YOUTUBE = "youtube"


class LiveStreamMeta(NamedTuple):
    """Title + category of a channel that is live right now (#233).

    Both are best-effort and nullable: a channel can be live with neither set.
    ``category`` is Twitch's ``game_name`` (e.g. "Age of Empires II"); YouTube
    has no equivalent, so a YouTube-sourced value is always ``None``. Compared
    by value, so the broadcast poller's change-detection treats a title- or
    category-only edit as a real change worth re-snapshotting and nudging on —
    while deliberately omitting volatile fields (viewer count) that would churn
    a nudge every cycle.
    """

    title: str | None
    category: str | None


_TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_TWITCH_STREAMS_URL = "https://api.twitch.tv/helix/streams"
# Helix /streams accepts up to 100 user_login params per call.
_TWITCH_STREAMS_BATCH = 100
# Re-mint the app token this many seconds before its stated expiry so an
# in-flight request never races the boundary.
_TOKEN_REFRESH_SKEW_SECONDS = 60

_YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
_YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"

# Twitch first-path segments that are site sections, not channel logins.
_TWITCH_RESERVED = {"videos", "directory", "p", "settings", "subscriptions", "downloads"}


def extract_stream_urls(presentation: dict) -> list[str]:
    """Pull candidate stream URLs out of a player's opaque presentation bag.

    Scans top-level string values and strings inside top-level lists, so
    the frontend's ``streamUrls: [...]`` list and any flat ``twitch`` /
    ``youtube`` keys both work without assuming a fixed schema. Non-URL
    strings (bio text, etc.) are harmless — the platform parsers return
    None for anything that isn't a twitch.tv / youtube.com URL.
    """
    urls: list[str] = []
    for value in presentation.values():
        if isinstance(value, str):
            urls.append(value)
        elif isinstance(value, list):
            urls.extend(item for item in value if isinstance(item, str))
    return urls


def parse_twitch_login(url: str) -> str | None:
    """Return the lowercase Twitch login from a twitch.tv URL, or None.

    ``https://www.twitch.tv/Grubby`` -> ``grubby``. Reserved site paths
    (``/videos``, ``/directory``, …) and non-twitch URLs return None.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.netloc.lower().removeprefix("www.") != "twitch.tv":
        return None
    segments = [s for s in parsed.path.split("/") if s]
    if not segments or segments[0].lower() in _TWITCH_RESERVED:
        return None
    return segments[0].lower()


def parse_youtube_ref(url: str) -> tuple[str, str] | None:
    """Return a ``(kind, value)`` reference to a YouTube channel, or None.

    ``kind`` is one of: ``channel_id`` (the UC… id from ``/channel/<id>``),
    ``handle`` (from ``/@handle``), or ``username`` (from ``/user/<name>``).
    Legacy ``/c/<name>`` and bare-vanity URLs aren't cheaply resolvable via
    the Data API, so they return None — those channels just get no live
    detection.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.netloc.lower().removeprefix("www.") not in ("youtube.com", "m.youtube.com"):
        return None
    segments = [s for s in parsed.path.split("/") if s]
    if not segments:
        return None
    first = segments[0]
    if first == "channel" and len(segments) > 1:
        return ("channel_id", segments[1])
    if first.startswith("@"):
        return ("handle", first)
    if first == "user" and len(segments) > 1:
        return ("username", segments[1])
    return None


class TwitchLiveClient:
    """Checks Twitch live status via Helix, holding an app access token.

    Uses the client-credentials grant (server-to-server) — one token for
    the whole app, lazily minted and cached until just before expiry, and
    re-minted on a 401. Live status is read by ``user_login`` directly:
    ``/streams`` returns only live channels, so the response *is* the live
    set. (Resolving logins to numeric ``user_id`` first would be more
    robust against handle renames; deferred — see #112.)
    """

    def __init__(self, client_id: str, client_secret: str, http: httpx.AsyncClient) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._token: str | None = None
        self._token_expiry: float = 0.0  # time.monotonic() seconds

    async def _app_token(self, *, force: bool = False) -> str:
        if not force and self._token is not None and time.monotonic() < self._token_expiry:
            return self._token
        response = await self._http.post(
            _TWITCH_TOKEN_URL,
            params={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "client_credentials",
            },
        )
        response.raise_for_status()
        body = response.json()
        self._token = body["access_token"]
        self._token_expiry = (
            time.monotonic() + body.get("expires_in", 0) - _TOKEN_REFRESH_SKEW_SECONDS
        )
        return self._token

    async def get_live_streams(self, logins: list[str]) -> dict[str, LiveStreamMeta]:
        """Return ``login -> LiveStreamMeta`` for the live subset of ``logins``.

        ``/streams`` returns only live channels, so the response *is* the live
        set — and it already carries each stream's ``title`` and ``game_name``
        (the category), so surfacing them costs no extra call (#233). Batched
        up to 100 per Helix call; logins compared lowercase. On a 401 the token
        is re-minted once and the batch retried.
        """
        live: dict[str, LiveStreamMeta] = {}
        for start in range(0, len(logins), _TWITCH_STREAMS_BATCH):
            live |= await self._live_batch(logins[start : start + _TWITCH_STREAMS_BATCH])
        return live

    async def _live_batch(self, batch: list[str]) -> dict[str, LiveStreamMeta]:
        response = await self._streams_request(batch, await self._app_token())
        if response.status_code == 401:
            response = await self._streams_request(batch, await self._app_token(force=True))
        response.raise_for_status()
        return {
            row["user_login"].lower(): LiveStreamMeta(
                # Helix sends "" (not null) for an unset title/category; fold to
                # None so the column is uniformly nullable across platforms.
                title=row.get("title") or None,
                category=row.get("game_name") or None,
            )
            for row in response.json().get("data", [])
            if row.get("type") == "live"
        }

    async def _streams_request(self, batch: list[str], token: str) -> httpx.Response:
        return await self._http.get(
            _TWITCH_STREAMS_URL,
            params=[("user_login", login) for login in batch],
            headers={"Authorization": f"Bearer {token}", "Client-Id": self._client_id},
        )


class YouTubeLiveClient:
    """Best-effort YouTube live detection via the Data API.

    Resolves each channel reference to a channelId (cached for the process
    lifetime — channelIds are stable), then checks live via ``search.list``.
    That call costs 100 quota units and has no batch form, so callers must
    keep the channel set small and the cadence slow (see
    ``run_youtube_live_poller``). Resolution failures and quota/HTTP errors
    degrade to "offline" rather than raising, so one bad channel can't
    stall the rest.
    """

    def __init__(self, api_key: str, http: httpx.AsyncClient) -> None:
        self._api_key = api_key
        self._http = http
        self._channel_id_cache: dict[tuple[str, str], str | None] = {}

    async def get_live_refs(
        self, refs: list[tuple[str, str]]
    ) -> dict[tuple[str, str], LiveStreamMeta]:
        """Return ``ref -> LiveStreamMeta`` for the channel ``refs`` live now.

        Mirrors the Twitch client's shape (#233). YouTube exposes no category,
        so ``category`` is always None; the live video's ``snippet.title`` fills
        ``title``. A resolution/quota failure degrades a ref to absent (offline).
        """
        live: dict[tuple[str, str], LiveStreamMeta] = {}
        for ref in refs:
            channel_id = await self._resolve_channel_id(ref)
            if channel_id is None:
                continue
            meta = await self._live_meta(channel_id)
            if meta is not None:
                live[ref] = meta
        return live

    async def _resolve_channel_id(self, ref: tuple[str, str]) -> str | None:
        if ref in self._channel_id_cache:
            return self._channel_id_cache[ref]
        kind, value = ref
        if kind == "channel_id":
            channel_id: str | None = value
        else:
            param = "forHandle" if kind == "handle" else "forUsername"
            channel_id = await self._lookup_channel_id(param, value)
        self._channel_id_cache[ref] = channel_id
        return channel_id

    async def _lookup_channel_id(self, param: str, value: str) -> str | None:
        try:
            response = await self._http.get(
                _YOUTUBE_CHANNELS_URL,
                params={"part": "id", param: value, "key": self._api_key},
            )
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("youtube_channel_lookup_failed", value=value, error=str(e))
            return None
        items = response.json().get("items", [])
        return items[0]["id"] if items else None

    async def _live_meta(self, channel_id: str) -> LiveStreamMeta | None:
        """Return the live video's metadata for ``channel_id``, or None if offline.

        ``search.list`` with ``part=snippet`` costs the same 100 quota units as
        ``part=id`` did but also returns the live video's title (#233). No live
        item means offline; an HTTP/quota error degrades to offline rather than
        raising, so one bad channel can't stall the rest.
        """
        try:
            response = await self._http.get(
                _YOUTUBE_SEARCH_URL,
                params={
                    "part": "snippet",
                    "channelId": channel_id,
                    "eventType": "live",
                    "type": "video",
                    "maxResults": 1,
                    "key": self._api_key,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("youtube_live_check_failed", channel_id=channel_id, error=str(e))
            return None
        items = response.json().get("items", [])
        if not items:
            return None
        title = items[0].get("snippet", {}).get("title") or None
        return LiveStreamMeta(title=title, category=None)


def build_broadcast_http_client() -> httpx.AsyncClient:
    """Shared httpx client for the Twitch + YouTube live clients.

    No ``base_url`` — both clients use absolute URLs (they span id.twitch.tv,
    api.twitch.tv, and googleapis.com). Built once in the lifespan and
    closed on shutdown, mirroring ``build_upstream_client``.
    """
    return httpx.AsyncClient(timeout=10.0, headers={"Accept": "application/json"})

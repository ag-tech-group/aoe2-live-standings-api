"""criticalbit-auth-api users client — fetches and caches user identity.

The standings API stores opaque ``user_id`` UUIDs in ``tournament_owners``
(the value of the access token's ``sub`` claim). Surfaces that need to
render the people behind those ids — the owners-list admin tab (#83) —
resolve them through this client against auth-api's ``/users/lookup``
endpoint.

Auth: ``/users/lookup`` requires a signed-in caller. Rather than invent a
service identity, this client forwards the caller's ``criticalbit_access``
cookie so the outbound call runs as the originating user. The caller is
already authenticated by ``require_tournament_owner`` upstream, so the
cookie is always present when this is invoked.

Caching: identity rarely changes mid-session and the same handful of ids
is requested over and over (a tournament has a small fixed roster of
owners), so a process-wide cache with a 60-second per-id TTL eliminates
the steady-state N+1 against auth-api without making the data stale
enough to mislead a human. Cache misses across a call are batched into a
single auth-api lookup.

Degradation: if the lookup fails (network blip, 5xx, auth-api down), the
function returns whatever it could resolve and logs the rest. Callers
treat absence as "no identity available" and emit null fields rather than
failing the whole response.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx
import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

# Per-id TTL on the identity cache. Tuned to the human-edit cadence of
# display name / avatar (rare, never urgent) while keeping the steady-state
# load on auth-api near zero — one cold call per owner per minute even
# under a hot-refresh loop on the admin tab.
_CACHE_TTL_SECONDS = 60.0

# httpx timeout for the lookup. Tight: we'd rather degrade to null
# enrichment than block the owners-list response on a slow auth-api.
_REQUEST_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class UserIdentity:
    """The subset of auth-api's UserLookupResult the standings API renders."""

    user_id: str
    email: str | None
    display_name: str | None
    avatar_url: str | None


# Process-wide cache. Keyed by user_id; value is (identity, expires_at).
# `expires_at` is a `time.monotonic()` reading so wall-clock changes don't
# misexpire entries.
_cache: dict[str, tuple[UserIdentity, float]] = {}
_cache_lock = asyncio.Lock()


async def fetch_identities(
    user_ids: list[str],
    *,
    access_token: str | None,
) -> dict[str, UserIdentity]:
    """Resolve a batch of user ids into identity records, served from cache when fresh.

    Cache misses (and entries past TTL) are batched into a single
    ``/users/lookup`` call. Unknown ids are silently absent from the
    returned mapping — auth-api's contract for ids it doesn't recognize.

    If the auth-api call fails, the cached subset is returned and the
    failure logged. Callers should treat absence as "no identity
    available", not "user doesn't exist".
    """
    if not user_ids:
        return {}

    # Dedupe before any cache work — a caller asking for the same id twice
    # shouldn't double-lookup.
    unique_ids = list(dict.fromkeys(user_ids))

    now = time.monotonic()
    fresh: dict[str, UserIdentity] = {}
    misses: list[str] = []

    async with _cache_lock:
        for uid in unique_ids:
            cached = _cache.get(uid)
            if cached is not None and cached[1] > now:
                fresh[uid] = cached[0]
            else:
                misses.append(uid)

    if not misses:
        return fresh

    try:
        fetched = await _lookup(misses, access_token=access_token)
    except Exception as exc:
        # Swallow + log: the caller still has the cached subset, and
        # the route returns nulls for whatever isn't there.
        logger.warning(
            "auth_api_lookup_failed",
            user_id_count=len(misses),
            error=str(exc),
        )
        return fresh

    async with _cache_lock:
        expires_at = time.monotonic() + _CACHE_TTL_SECONDS
        for identity in fetched:
            _cache[identity.user_id] = (identity, expires_at)
            fresh[identity.user_id] = identity

    return fresh


async def _lookup(
    user_ids: list[str],
    *,
    access_token: str | None,
) -> list[UserIdentity]:
    """Call auth-api ``GET /users/lookup`` for the given ids.

    Raises on transport / non-2xx errors so callers can decide on
    degradation (``fetch_identities`` swallows and logs).
    """
    url = f"{settings.auth_api_base_url.rstrip('/')}/users/lookup"
    # httpx prefers cookies set on the client instance — per-request cookies
    # are deprecated. An empty cookie jar means we won't even send a Cookie
    # header, which is what we want when the caller has no access token.
    cookies = {"criticalbit_access": access_token} if access_token else None

    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS, cookies=cookies) as client:
        response = await client.get(url, params={"ids": user_ids})
        response.raise_for_status()
        payload = response.json()

    return [
        UserIdentity(
            user_id=str(item["id"]),
            email=item.get("email"),
            display_name=item.get("display_name"),
            avatar_url=item.get("avatar_url"),
        )
        for item in payload
    ]


def reset_cache() -> None:
    """Drop the cache — used by tests so one test's identities don't leak."""
    _cache.clear()

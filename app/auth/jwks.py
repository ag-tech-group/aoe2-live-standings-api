"""JWKS client — fetches and caches criticalbit-auth-api's signing key.

The write API verifies access-token JWTs with the RSA public key published
at ``settings.auth_jwks_url``. The auth service publishes a single key and
rotates it rarely, so the key is fetched once and cached process-wide;
``get_public_key(force_refresh=True)`` re-fetches it after a rotation (the
verifier retries once when a signature first fails).
"""

from __future__ import annotations

import httpx
import structlog
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from jwt.algorithms import RSAAlgorithm

from app.config import settings

logger = structlog.get_logger(__name__)

# Process-wide cache of the parsed public key. `None` means "not fetched yet".
_cached_public_key: RSAPublicKey | None = None


async def _fetch_jwks() -> dict:
    """Fetch the raw JWKS document from the auth API."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        logger.info("jwks_fetch", url=settings.auth_jwks_url)
        response = await client.get(settings.auth_jwks_url)
        response.raise_for_status()
        return response.json()


async def _load_public_key() -> RSAPublicKey:
    """Fetch the JWKS and parse its first RSA public key.

    The auth service's JWKS carries exactly one key, so the first entry is
    the signing key; a rotation replaces it wholesale.
    """
    jwks = await _fetch_jwks()
    keys = jwks.get("keys", [])
    if not keys:
        raise ValueError("JWKS response contained no keys")
    public_key = RSAAlgorithm.from_jwk(keys[0])
    if not isinstance(public_key, RSAPublicKey):
        raise ValueError("JWKS key is not an RSA public key")
    logger.info("jwks_loaded", kid=keys[0].get("kid"))
    return public_key


async def get_public_key(*, force_refresh: bool = False) -> RSAPublicKey:
    """Return the cached RSA public key, fetching it on first use or on refresh."""
    global _cached_public_key
    if _cached_public_key is None or force_refresh:
        _cached_public_key = await _load_public_key()
    return _cached_public_key


def reset_cache() -> None:
    """Drop the cached key — used by tests so one test's key can't leak into the next."""
    global _cached_public_key
    _cached_public_key = None

"""FastAPI authentication and authorization dependencies.

The read surface is public; the write/management surface is gated here.
``get_current_user_id`` authenticates a request — it verifies the
``criticalbit_access`` cookie's JWT against criticalbit-auth-api's JWKS and
yields the criticalbit user UUID. ``require_tournament_owner`` authorizes
one — it confirms that user owns the tournament named in the path.
"""

from __future__ import annotations

import jwt
import structlog
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwks import get_public_key
from app.config import settings
from app.database import get_async_session
from app.models import Tournament, TournamentOwner

logger = structlog.get_logger(__name__)

# The cookie criticalbit-auth-api sets its RS256 access token in. Scoped to
# `.criticalbit.gg`, so the browser sends it to every platform subdomain.
ACCESS_TOKEN_COOKIE = "criticalbit_access"

# fastapi-users signs every access token with this audience; the auth
# service uses the library default, so the verifier must expect it.
TOKEN_AUDIENCE = ["fastapi-users:auth"]


def _decode_kwargs() -> dict:
    """Common ``jwt.decode`` kwargs; the issuer is enforced only when configured."""
    kwargs: dict = {"algorithms": ["RS256"], "audience": TOKEN_AUDIENCE}
    if settings.auth_token_issuer:
        kwargs["issuer"] = settings.auth_token_issuer
    return kwargs


async def get_current_user_id(request: Request) -> str:
    """Verify the access-token cookie and return the criticalbit user UUID.

    Raises 401 when the cookie is absent or the token fails verification.
    A signature failure triggers one JWKS refresh + retry, so a key
    rotation doesn't spuriously reject otherwise-valid tokens.
    """
    token = request.cookies.get(ACCESS_TOKEN_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    decode_kwargs = _decode_kwargs()
    try:
        payload = jwt.decode(token, key=await get_public_key(), **decode_kwargs)
    except jwt.InvalidSignatureError:
        logger.warning("jwt_signature_invalid", detail="refreshing JWKS and retrying")
        try:
            payload = jwt.decode(
                token, key=await get_public_key(force_refresh=True), **decode_kwargs
            )
        except jwt.PyJWTError as e:
            logger.warning("jwt_verify_failed", error=str(e))
            raise HTTPException(status_code=401, detail="Invalid token") from e
    except jwt.PyJWTError as e:
        logger.warning("jwt_verify_failed", error=str(e))
        raise HTTPException(status_code=401, detail="Invalid token") from e

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token: missing subject")

    structlog.contextvars.bind_contextvars(user_id=user_id)
    return user_id


async def require_tournament_owner(
    tournament_slug: str,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_async_session),
) -> Tournament:
    """Resolve the path's tournament and confirm the caller owns it.

    The single gate on every write route: 401 if the request carries no
    valid token, 404 if the slug names no tournament, 403 if the
    authenticated user has no owner row for it. Returns the resolved
    ``Tournament`` so handlers get the access check and the row at once.
    """
    tournament = (
        await session.execute(select(Tournament).where(Tournament.slug == tournament_slug))
    ).scalar_one_or_none()
    if tournament is None:
        raise HTTPException(status_code=404, detail="Tournament not found")

    owner = (
        await session.execute(
            select(TournamentOwner.user_id).where(
                TournamentOwner.tournament_id == tournament.id,
                TournamentOwner.user_id == user_id,
            )
        )
    ).first()
    if owner is None:
        raise HTTPException(status_code=403, detail="Not a tournament owner")

    return tournament

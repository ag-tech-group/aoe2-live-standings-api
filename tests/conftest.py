from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import leaderboards_cache
from app.auth import get_current_user_id, jwks
from app.database import Base, get_async_session
from app.events import hub
from app.main import app, limiter
from app.models import (
    Match,
    MatchOutcome,
    MatchPlayer,
    MatchState,
    Player,
    PlayerRating,
    Team,
    TeamMember,
    Tournament,
    TournamentOwner,
    TournamentPlayer,
)

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_database():
    """Create tables before each test, drop after."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Clear rate-limit buckets between tests so request counts don't leak across tests."""
    limiter._storage.reset()
    yield
    limiter._storage.reset()


@pytest.fixture(autouse=True)
def reset_leaderboards_cache():
    """Drop any leaderboard metadata a test wrote into the module-level cache."""
    yield
    leaderboards_cache.clear_cache()


@pytest.fixture(autouse=True)
def reset_event_hub():
    """Drop any SSE subscribers a test left registered on the module-level hub."""
    yield
    hub._subscribers.clear()


@pytest.fixture(autouse=True)
def stub_jwks(monkeypatch: pytest.MonkeyPatch):
    """Resolve JWKS to the test public key offline, for every test.

    Patches the key loader so both the first fetch and the force-refresh
    retry return the in-process test key — no test reaches the network for
    JWKS, and tokens minted by ``make_access_token`` verify.
    """

    async def _load_test_key():
        return _test_public_key

    monkeypatch.setattr(jwks, "_load_public_key", _load_test_key)
    jwks.reset_cache()
    yield
    jwks.reset_cache()


async def override_get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session


app.dependency_overrides[get_async_session] = override_get_async_session


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client for testing."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    """Direct database session for test setup."""
    async with async_session_maker() as session:
        yield session


@pytest.fixture
def auth_as():
    """Authenticate the test client as a given criticalbit user id.

    Returns a function: call ``auth_as(user_id)`` in a test to act as that
    user. It overrides ``get_current_user_id`` so write-endpoint tests
    needn't mint a JWT; the override is cleared afterwards. Tests that
    exercise the real token path (``test_auth.py``) simply don't use it.
    """

    def _auth_as(user_id: str) -> None:
        app.dependency_overrides[get_current_user_id] = lambda: user_id

    yield _auth_as
    app.dependency_overrides.pop(get_current_user_id, None)


# ---------------------------------------------------------------------------
# Factory helpers.
#
# Plain functions (not fixtures) returning unsaved ORM instances. Tests
# call `session.add(make_player(...))` and commit themselves — keeps the
# call site obvious and lets one test build a graph of related rows
# without juggling fixture dependencies.
# ---------------------------------------------------------------------------


def make_player(profile_id: int, **overrides: Any) -> Player:
    """Build a Player with reasonable defaults; override any field via kwargs."""
    defaults: dict[str, Any] = {
        "profile_id": profile_id,
        "alias": f"player_{profile_id}",
        "country": "ca",
        "steam_id": None,
        "level": 1,
        "xp": 0,
        "region_id": 0,
        "clan_name": None,
    }
    defaults.update(overrides)
    return Player(**defaults)


def make_player_rating(profile_id: int, leaderboard_id: int, **overrides: Any) -> PlayerRating:
    """Build a PlayerRating with reasonable defaults."""
    defaults: dict[str, Any] = {
        "profile_id": profile_id,
        "leaderboard_id": leaderboard_id,
        "current_rating": 1500,
        "max_rating": 1500,
        "wins": 0,
        "losses": 0,
        "streak": 0,
        "drops": 0,
        "rank": None,
        "rank_total": None,
        "region_rank": None,
        "region_rank_total": None,
        "last_match_at": None,
    }
    defaults.update(overrides)
    return PlayerRating(**defaults)


def make_match(match_id: int, **overrides: Any) -> Match:
    """Build a completed Match with reasonable defaults."""
    defaults: dict[str, Any] = {
        "match_id": match_id,
        "map_name": "Arabia.rms",
        "matchtype_id": 6,
        "leaderboard_id": 3,
        "started_at": datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC),
        "completed_at": datetime(2026, 5, 18, 12, 30, 0, tzinfo=UTC),
        "description": None,
        "state": MatchState.COMPLETED,
    }
    defaults.update(overrides)
    return Match(**defaults)


def make_match_player(match_id: int, profile_id: int, **overrides: Any) -> MatchPlayer:
    """Build a MatchPlayer (winner) with reasonable defaults."""
    defaults: dict[str, Any] = {
        "match_id": match_id,
        "profile_id": profile_id,
        "civilization_id": 0,
        "team_id": 0,
        "outcome": MatchOutcome.WIN,
        "old_rating": 1500,
        "new_rating": 1510,
        "xp_gained": 1,
    }
    defaults.update(overrides)
    return MatchPlayer(**defaults)


def make_tournament(
    slug: str,
    profile_ids: list[int] | None = None,
    owner_ids: list[str] | None = None,
    **overrides: Any,
) -> Tournament:
    """Build a Tournament with reasonable defaults, an optional roster, and owners.

    ``profile_ids`` seeds the tracked roster (``TournamentPlayer``);
    ``owner_ids`` seeds the criticalbit user ids authorized to manage the
    tournament (``TournamentOwner``).
    """
    defaults: dict[str, Any] = {
        "slug": slug,
        "name": f"Tournament {slug}",
        "leaderboard_id": 3,
        "start_date": None,
        "end_date": None,
    }
    defaults.update(overrides)
    tournament = Tournament(**defaults)
    tournament.tracked_players = [TournamentPlayer(profile_id=pid) for pid in (profile_ids or [])]
    tournament.owners = [TournamentOwner(user_id=uid) for uid in (owner_ids or [])]
    return tournament


def make_team(name: str, profile_ids: list[int] | None = None, **overrides: Any) -> Team:
    """Build a Team with reasonable defaults and an optional member list.

    Attach to a tournament via ``tournament.teams = [...]`` so its
    ``tournament_id`` and ``id`` are assigned on flush.
    """
    defaults: dict[str, Any] = {
        "name": name,
        "initials": name[:8].upper(),
    }
    defaults.update(overrides)
    team = Team(**defaults)
    team.members = [TeamMember(profile_id=pid) for pid in (profile_ids or [])]
    return team


# ---------------------------------------------------------------------------
# Auth test helpers.
#
# A throwaway RSA keypair stands in for criticalbit-auth-api's signing key:
# `make_access_token` mints tokens with the private half, and the autouse
# `stub_jwks` fixture serves the public half so verification runs offline.
# ---------------------------------------------------------------------------

_test_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_test_public_key = _test_private_key.public_key()

# A default criticalbit user UUID for tests that don't care about identity.
DEFAULT_TEST_USER_ID = "00000000-0000-0000-0000-0000000000aa"


def make_access_token(user_id: str = DEFAULT_TEST_USER_ID, **claim_overrides: Any) -> str:
    """Mint an RS256 access token signed with the test key.

    Defaults to a valid 15-minute token for ``user_id`` carrying the
    audience criticalbit-auth-api uses. Pass overrides to forge the
    rejection cases — e.g. ``exp=<past datetime>``, ``aud="urn:wrong"``,
    or ``sub=None`` to drop the subject claim entirely.
    """
    now = datetime.now(tz=UTC)
    claims: dict[str, Any] = {
        "sub": user_id,
        "aud": ["fastapi-users:auth"],
        "iat": now,
        "exp": now + timedelta(minutes=15),
    }
    claims.update(claim_overrides)
    # An override of None means "omit this claim" (used to drop `sub`).
    claims = {key: value for key, value in claims.items() if value is not None}
    return jwt.encode(claims, _test_private_key, algorithm="RS256")

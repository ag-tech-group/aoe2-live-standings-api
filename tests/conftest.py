from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import leaderboards_cache
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
    Tournament,
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
    slug: str, profile_ids: list[int] | None = None, **overrides: Any
) -> Tournament:
    """Build a Tournament with reasonable defaults and an optional roster."""
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
    return tournament

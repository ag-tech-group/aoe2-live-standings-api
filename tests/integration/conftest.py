"""Integration tests against a real Postgres via testcontainers.

These promote the manual smoke test (docker compose + uvicorn + curl) to a
deterministic CI check. The session-scoped container holds the schema;
each test truncates tables to start clean.

Cleanly skipped if Docker isn't running on the host — `uv run pytest`
still works for contributors without docker, they just don't run these.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest

# Pre-flight Docker check at conftest import time. testcontainers will
# fail noisily later if Docker isn't available; we'd rather skip the
# whole module cleanly so the rest of the test suite stays unaffected.
_DOCKER_AVAILABLE = False
try:
    import docker as _docker_sdk

    _docker_sdk.from_env().ping()
    _DOCKER_AVAILABLE = True
except Exception:
    _DOCKER_AVAILABLE = False

if not _DOCKER_AVAILABLE:
    pytest.skip(
        "Docker not available; skipping integration tests",
        allow_module_level=True,
    )

# Imports below depend on testcontainers/asgi-lifespan — deferred until
# after the Docker check so a missing-docker host doesn't surface as a
# confusing ImportError on the testcontainers package.
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.postgres import PostgresContainer  # noqa: E402

from app import database  # noqa: E402
from app.database import Base, get_async_session  # noqa: E402
from app.main import app  # noqa: E402 — also triggers model registration via the import chain

# Pin to the same image docker-compose uses locally so dev and CI Postgres
# stay in lockstep. Driver=asyncpg makes get_connection_url() return a URL
# SQLAlchemy's async engine accepts directly.
_POSTGRES_IMAGE = "postgres:16-alpine"


async def _apply_schema(url: str) -> None:
    """Run ``Base.metadata.create_all`` once against the container."""
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


@pytest.fixture(scope="session")
def postgres_container():
    """One Postgres container per pytest session — booted once, reused.

    Schema setup runs in its own ``asyncio.run`` event loop so this stays
    a synchronous session-scoped fixture, sidestepping pytest-asyncio's
    fixture-vs-test loop-scope coupling.
    """
    with PostgresContainer(_POSTGRES_IMAGE, driver="asyncpg") as pg:
        asyncio.run(_apply_schema(pg.get_connection_url()))
        yield pg


@pytest.fixture
async def patched_engine(postgres_container, monkeypatch):
    """Per-test asyncpg engine + monkey-patched ``app.database`` module.

    The poller tasks use ``app.database.async_session_maker`` directly
    (not via DI), so we have to swap it out at the module level for the
    duration of the test. ``monkeypatch`` restores both attributes on
    teardown.
    """
    url = postgres_container.get_connection_url()
    engine = create_async_engine(url)
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "async_session_maker", session_maker)

    yield engine, session_maker

    await engine.dispose()


@pytest.fixture(autouse=True)
async def setup_database():
    """Shadow the parent conftest's SQLite ``setup_database`` autouse.

    Integration tests never touch the parent's SQLite engine; the parent
    fixture would still create + drop in-memory tables on every test if
    we didn't replace it. A no-op here keeps this suite Postgres-only.
    """
    yield


@pytest.fixture(autouse=True)
async def clean_postgres_tables(patched_engine) -> AsyncGenerator[None, None]:
    """TRUNCATE every table before each test so cases start with empty rows.

    ``RESTART IDENTITY CASCADE`` keeps the cross-table FKs (matches →
    match_players, players → player_ratings) handled in one statement.
    """
    engine, _ = patched_engine
    async with engine.begin() as conn:
        table_names = ", ".join(t.name for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
async def pg_session(patched_engine) -> AsyncGenerator[AsyncSession, None]:
    """An ``AsyncSession`` bound to the testcontainer Postgres."""
    _, session_maker = patched_engine
    async with session_maker() as session:
        yield session


@pytest.fixture
async def pg_client(patched_engine, monkeypatch) -> AsyncGenerator[AsyncClient, None]:
    """An httpx ASGI client whose ``get_async_session`` DI hits the container.

    ``monkeypatch.setitem`` keeps the override scoped to this test — the
    parent conftest's SQLite override is restored automatically on
    teardown, so unit tests in the same pytest session aren't affected.
    """
    _, session_maker = patched_engine

    async def _override_get_async_session():
        async with session_maker() as session:
            yield session

    monkeypatch.setitem(app.dependency_overrides, get_async_session, _override_get_async_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
def patched_session_maker(patched_engine):
    """Convenience accessor — the session_maker used directly by poller ticks."""
    _, session_maker = patched_engine
    return session_maker

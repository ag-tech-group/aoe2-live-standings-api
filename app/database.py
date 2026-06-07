from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""

    pass


# Per-instance pool size is role-derived. The read (api) tier is
# listener-only and mostly serves SSE (which holds no DB connection) plus
# CDN-cached reads, so a minimal pool suffices — and it's the tier that
# scales out to many instances, so a smaller per-instance pool directly
# lowers the Cloud SQL connection budget. When the connector path is on
# (#196), this pool sizes the api instance's *client* connections to the MCP
# pooler — cheap, and the pooler multiplexes them onto a few server backends,
# so instance count no longer drives `num_backends`. When off, it sizes
# direct server connections (the 200-cap budget). The polling (worker) tier
# runs several concurrent pollers so it keeps pool_size=2, but it's a single
# instance (min=max=1) so its pool barely moves the budget. Mono/dev gets 2.
_read_only_tier = settings.listener_enabled and not settings.polling_enabled

# Shared pool tuning, independent of how the connection is established.
# `pool_pre_ping` runs a `SELECT 1` before each checkout so a connection the
# server closed (Cloud SQL idle timeout, or a pooler recycle) is replaced
# rather than handed to a request that then errors; `pool_recycle` retires
# connections before the server-side timeout fires.
_POOL_KWARGS = {
    "echo": settings.is_development,
    "pool_size": 1 if _read_only_tier else 2,
    "max_overflow": 1,
    "pool_pre_ping": True,
    "pool_recycle": 1800,
}


def _build_engine() -> AsyncEngine:
    """Build the request-query engine.

    Two modes:
    - **Direct** (default): connect via ``settings.database_url`` — the unix
      socket in prod, a TCP DSN in dev/tests. Used by the worker too.
    - **Connector → pooler** (``db_use_connector``): connect through the Cloud
      SQL Python connector to the instance's Managed Connection Pooling
      transaction pooler. asyncpg's client-side statement cache and the
      SQLAlchemy asyncpg dialect's prepared-statement cache are both disabled —
      under transaction pooling a prepared statement created on one server
      connection can't be reused on another, so caching them corrupts pooled
      sessions. The connector is created lazily on the first checkout (inside
      the running event loop, where ``create_async_connector`` works), then
      reused for the life of the process.
    """
    if not settings.db_use_connector:
        return create_async_engine(settings.database_url, **_POOL_KWARGS)

    from google.cloud.sql.connector import create_async_connector

    # One connector per process, created on first use (closed over via
    # nonlocal so the lazy init survives across getconn calls).
    connector = None

    async def getconn():
        nonlocal connector
        if connector is None:
            connector = await create_async_connector()
        return await connector.connect_async(
            settings.db_instance_connection_name,
            "asyncpg",
            user=settings.db_user,
            password=settings.db_password,
            db=settings.db_name,
            # asyncpg-level: disable the client-side prepared-statement cache.
            statement_cache_size=0,
        )

    return create_async_engine(
        # No host/credentials in the URL — the connector supplies the raw
        # connection via async_creator. The query arg disables the SQLAlchemy
        # asyncpg dialect's own prepared-statement cache (honored from the URL
        # even when async_creator is set).
        "postgresql+asyncpg://?prepared_statement_cache_size=0",
        async_creator=getconn,
        **_POOL_KWARGS,
    )


engine = _build_engine()

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency that provides an async database session."""
    async with async_session_maker() as session:
        yield session

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""

    pass


# Per-instance pool size is role-derived. The read (api) tier is
# listener-only and mostly serves SSE (which holds no DB connection) plus
# CDN-cached reads, so a minimal pool suffices — and it's the tier that
# scales out to many instances, so a smaller per-instance pool directly
# lowers the Cloud SQL connection budget (200-connection cap on the
# db-custom-1-3840 tier). Critically, dead-tab SSE streams pin *idle* api
# instances (#204), and an idle instance holds its base pool (pool_size) —
# so dropping the read tier to pool_size=1 cuts that pinned footprint to
# 1 pool + 1 LISTEN = 2 conns/instance (was 3); at maxScale=22 that's ~44
# vs ~66 api backends. The polling (worker) tier runs several concurrent
# pollers so it keeps pool_size=2, but it's a single instance (min=max=1)
# so its pool barely moves the budget. Mono/dev (both flags set) gets 2.
# This replaces PgBouncer as the cheap first lever (#196 ruled it out:
# managed pooling needs Enterprise Plus; self-hosting fits Cloud Run
# poorly). Next lever if needed: a Cloud SQL tier bump (more max_connections).
_read_only_tier = settings.listener_enabled and not settings.polling_enabled

engine = create_async_engine(
    settings.database_url,
    echo=settings.is_development,
    pool_size=1 if _read_only_tier else 2,
    # One burst slot for concurrent query load on top of the base pool.
    max_overflow=1,
    # Cloud SQL closes idle connections server-side after some
    # minutes; `pool_pre_ping` runs a `SELECT 1` before each checkout
    # so a stale connection is replaced rather than handed to a
    # request that then errors. `pool_recycle` retires connections
    # before the server-side timeout fires.
    pool_pre_ping=True,
    pool_recycle=1800,
)

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency that provides an async database session."""
    async with async_session_maker() as session:
        yield session

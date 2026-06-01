from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""

    pass


engine = create_async_engine(
    settings.database_url,
    echo=settings.is_development,
    # Pool tuning for the Cloud SQL `db-g1-small` tier
    # (`max_connections=100`) crossed with the api service's
    # `max_instance_count=20` event-window scaling (#84) plus the
    # worker's 1 pinned instance. SQLAlchemy defaults
    # (pool_size=5 + max_overflow=10) would put the configured ceiling
    # at 21 × 15 = 315 — far over the DB cap.
    #
    # 2+1 = 3 connections per instance puts peak demand at 21 × 3 + 5
    # (migrate-job overlap) = 68 connections, leaving ~25 head before
    # the DB's `superuser_reserved_connections` floor refuses new
    # connections. The 2026-06-01 outage (see
    # `[[project_cloud_run_revision_outage]]`) ran at the previous
    # 3+2 = 5/instance budget; that math fit only when the CI prune
    # step kept one revision per service alive (now true). Tightening
    # to 3/instance buys belt-and-suspenders headroom so we tolerate
    # both (a) the cap being briefly bypassed by a prune miss, and
    # (b) reverting the FE's emergency `maxScale=10` cap to the
    # original event-window 20 without re-hitting the ceiling.
    # Pool exhaustion now surfaces as a brief request wait (not an
    # outage) — the next levers are PgBouncer (multiplex many app
    # conns onto few DB conns) and/or a Cloud SQL tier bump.
    pool_size=2,
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

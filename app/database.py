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
    # Pool tuning for the Cloud SQL `db-f1-micro` tier (~25 connection
    # default cap) crossed with the api service's max=10 scaling
    # (#47) plus the worker's 1 pinned instance. SQLAlchemy defaults
    # (pool_size=5 + max_overflow=10) would put the configured ceiling
    # at 11 × 15 = 165 — well over the DB's cap. These numbers cap
    # peak demand at 11 × 5 = 55, sustainable on the current tier.
    pool_size=3,
    max_overflow=2,
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

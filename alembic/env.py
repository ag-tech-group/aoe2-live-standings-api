import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.config import settings
from app.database import Base

# Models must be imported here so Alembic autogenerate can detect them via
# Base.metadata.
from app.models import (  # noqa: F401
    IdempotencyKey,
    Leaderboard,
    LiveMatchPlayer,
    Match,
    MatchPlayer,
    Player,
    PlayerRating,
    Team,
    TeamMember,
    Tournament,
    TournamentOwner,
    TournamentPlayer,
)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Set the database URL from our settings (not from alembic.ini).
# Transaction-pooling safety (#196): under Managed Connection Pooling the
# /cloudsql socket routes to the transaction pooler, where cached prepared
# statements collide across pooled server connections. Disable the SQLAlchemy
# asyncpg dialect's prepared-statement cache here (and asyncpg's own client
# cache via connect_args in run_async_migrations). Harmless on a direct,
# non-pooled connection (dev/CI Postgres), so it's applied unconditionally.
_db_url = settings.database_url
_db_url += ("&" if "?" in _db_url else "?") + "prepared_statement_cache_size=0"
config.set_main_option("sqlalchemy.url", _db_url)

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# MetaData object for 'autogenerate' support
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # asyncpg client-side statement cache off — see the URL note above
        # (transaction-pooling safety under MCP).
        connect_args={"statement_cache_size": 0},
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

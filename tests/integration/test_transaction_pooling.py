"""Transaction-pooling safety for the connector path (#196).

Cloud SQL Managed Connection Pooling is PgBouncer-derived and runs in
``transaction`` mode, where a prepared statement created on one server
connection can't be reused on another. The connector engine in
``app/database.py`` defends against that by disabling both asyncpg's
client-side statement cache (``statement_cache_size=0``) and the SQLAlchemy
asyncpg dialect's prepared-statement cache (``prepared_statement_cache_size=0``).

The SQLite unit suite can't exercise any of this. This stands up a real
PgBouncer (transaction mode) in front of Postgres — a faithful local stand-in
for MCP, no cloud credentials — and asserts that parameterized queries survive
transaction pooling with those flags, even when many clients multiplex onto a
tiny server pool (the collision case). (The app no longer uses LISTEN/NOTIFY —
#196 Option B replaced it with polling the ``nudge_versions`` table through the
pooler — so the pooled query path is the only thing that needs this guard.)

Auto-skipped when Docker is unavailable (see ``tests/integration/conftest.py``).
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network
from testcontainers.core.waiting_utils import wait_for_logs
from testcontainers.postgres import PostgresContainer

_PG_IMAGE = "postgres:16-alpine"
_PGBOUNCER_IMAGE = "edoburu/pgbouncer:v1.23.1-p3"
_USER = "test"
_PASSWORD = "test"  # noqa: S105 — throwaway local container credential
_DB = "test"


@pytest.fixture(scope="module")
def pooled_and_direct_dsns():
    """Postgres + a transaction-mode PgBouncer in front of it, on one network.

    Yields ``(pooled_url, direct_url)``: ``pooled_url`` hits PgBouncer (the MCP
    stand-in); ``direct_url`` hits Postgres directly. ``DEFAULT_POOL_SIZE=2``
    keeps the server pool tiny so concurrent clients are forced to reuse server
    connections across statements — exactly where prepared-statement caching
    would collide.
    """
    with Network() as net:
        postgres = (
            PostgresContainer(
                _PG_IMAGE, username=_USER, password=_PASSWORD, dbname=_DB, driver="asyncpg"
            )
            .with_network(net)
            .with_network_aliases("pg")
        )
        with postgres:
            bouncer = (
                DockerContainer(_PGBOUNCER_IMAGE)
                .with_env("DB_HOST", "pg")
                .with_env("DB_PORT", "5432")
                .with_env("DB_USER", _USER)
                .with_env("DB_PASSWORD", _PASSWORD)
                .with_env("DB_NAME", _DB)
                .with_env("POOL_MODE", "transaction")
                .with_env("AUTH_TYPE", "scram-sha-256")
                .with_env("MAX_CLIENT_CONN", "100")
                .with_env("DEFAULT_POOL_SIZE", "2")
                .with_network(net)
                # The edoburu image's PgBouncer listens on 5432 (drop-in port),
                # not the usual 6432 — see its generated config.
                .with_exposed_ports(5432)
            )
            with bouncer:
                wait_for_logs(bouncer, "listening on", timeout=60)
                host = bouncer.get_container_host_ip()
                port = bouncer.get_exposed_port(5432)
                pooled = f"postgresql+asyncpg://{_USER}:{_PASSWORD}@{host}:{port}/{_DB}"
                yield pooled, postgres.get_connection_url()


@pytest.mark.asyncio
async def test_parameterized_queries_survive_transaction_pooling(pooled_and_direct_dsns):
    """Many concurrent parameterized queries through the pooler must not raise.

    Mirrors the connector engine's flags: ``statement_cache_size=0`` (asyncpg)
    + ``prepared_statement_cache_size=0`` (dialect, via the URL). Without them,
    server-connection reuse under transaction pooling raises
    ``DuplicatePreparedStatementError`` — this is the regression guard.
    """
    pooled, _ = pooled_and_direct_dsns
    engine = create_async_engine(
        pooled + "?prepared_statement_cache_size=0",
        connect_args={"statement_cache_size": 0},
        pool_size=10,
        max_overflow=10,
    )
    try:

        async def run_one(n: int) -> int:
            async with engine.connect() as conn:
                result = await conn.execute(text("SELECT CAST(:n AS INTEGER) AS n"), {"n": n})
                return result.scalar_one()

        results = await asyncio.gather(*[run_one(i) for i in range(40)])
        assert sorted(results) == list(range(40))
    finally:
        await engine.dispose()

"""Fixtures for the `pipeline` level — the real composition against a real database.

Deliberately NOT skippable, like the `db` level: a suite that skips when
Postgres is missing reports green while proving nothing.

Isolation is by clean slate, not by per-run prefix: queues, outbox, webhook
events and heartbeats are shared state, not rows a prefix can partition. Every
test starts from empty and the teardown leaves empty — the anticipated
cenário 15, here because the suite grows in this milestone and two colliding
runs are the bug that only shows up on the worst day.
"""

import asyncio
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

import psycopg
import pytest
from psycopg import sql

from agents_runtime.config import QueueingConfig, config_from_env
from agents_runtime.queueing import ALL_QUEUES, INBOUND
from agents_runtime.repository.queue import PgmqQueue
from tests.support.database import dsn_from_env
from tests.support.fake_channel import SCHEMA_SQL
from tests.support.holdable import GATE_SQL
from tests.support.runtime_process import TINY_INTERVALS

if sys.platform == "win32":
    # psycopg's async connections need a selector loop and Windows defaults to
    # proactor. The hook must NOT exist elsewhere: pytest-asyncio treats "no
    # implementation" and "an implementation that declines" differently
    # (decisão 28).

    def pytest_asyncio_loop_factories(config: pytest.Config, item: pytest.Item) -> dict:
        return {"selector": asyncio.SelectorEventLoop}


@pytest.fixture(scope="session")
def dsn() -> str:
    return dsn_from_env()


@pytest.fixture
async def admin(dsn: str) -> AsyncIterator[psycopg.AsyncConnection]:
    """Superuser connection — arranges and inspects, never the subject of a test."""
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture
def sync_admin(dsn: str) -> Iterator[psycopg.Connection]:
    """The same superuser, synchronous — what the factories speak."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture(scope="session")
def _testing_schema(dsn: str) -> None:
    """The fake channel's tables. Test-only, created here, never by a migration."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SCHEMA_SQL)
        conn.execute(GATE_SQL)


@pytest.fixture(autouse=True)
def clean_slate(dsn: str, _testing_schema: None) -> Iterator[None]:
    """Empty world before and after every test."""

    def wipe() -> None:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for queue in ALL_QUEUES:
                conn.execute("select pgmq.purge_queue(%s)", (queue,))
                conn.execute(
                    sql.SQL("delete from pgmq.{}").format(sql.Identifier(f"a_{queue}"))
                )
            conn.execute("truncate internal.message_outbox, internal.webhook_events cascade")
            conn.execute("truncate internal.runtime_heartbeats")
            conn.execute(
                "truncate testing.fake_channel_sends, testing.fake_channel_directives,"
                " testing.responder_gate"
            )
            # Cascades through contacts, conversations, messages and accounts.
            conn.execute("truncate public.tenants cascade")

    wipe()
    yield
    wipe()


@pytest.fixture
def tiny_config() -> QueueingConfig:
    """The canonical shape at test speed — the same env the subprocess gets."""
    return config_from_env(dict(TINY_INTERVALS))


@pytest.fixture
async def queue(dsn: str) -> AsyncIterator[PgmqQueue]:
    """The inbound queue as the runtime holds it: as `worker_role`, not superuser."""
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute("set role worker_role")
        yield PgmqQueue(conn, INBOUND)


@pytest.fixture
def queue_length(dsn: str) -> Callable[..., Awaitable[int]]:
    """Rows in a queue table, visible or not — the measure of "nothing in limbo"."""

    async def measure(name: str = INBOUND) -> int:
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            cursor = await conn.execute(
                "select queue_length from pgmq.metrics(%s)", (name,)
            )
            row = await cursor.fetchone()
            assert row is not None
            return int(row[0])

    return measure

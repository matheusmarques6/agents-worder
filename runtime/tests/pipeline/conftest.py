"""Fixtures for the `pipeline` level — the real loop against a real pgmq.

Like the `db` level, these tests are deliberately NOT skippable: a queue suite
that skips when Postgres is missing reports green while proving nothing.

Isolation here cannot use the per-run prefix the `db` level uses — a queue is
shared state, not a row — so each test starts from an empty `q_inbound` and
leaves it empty.
"""

import asyncio
import sys
from collections.abc import AsyncIterator, Awaitable, Callable

import psycopg
import pytest

from agents_runtime.queueing import INBOUND
from agents_runtime.repository.queue import PgmqQueue
from tests.support.database import dsn_from_env


def pytest_asyncio_loop_factories(config: pytest.Config, item: pytest.Item) -> dict | None:
    """psycopg's async connections need a selector loop; Windows defaults to proactor.

    Production runs on Linux, where the selector loop is already the default —
    this only keeps the suite runnable on the development machine.
    """
    if sys.platform != "win32":
        return None
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
async def empty_inbound(admin: psycopg.AsyncConnection) -> AsyncIterator[None]:
    await _purge(admin)
    yield
    await _purge(admin)


async def _purge(conn: psycopg.AsyncConnection) -> None:
    await conn.execute("select pgmq.purge_queue(%s)", (INBOUND,))
    await conn.execute("delete from pgmq.a_q_inbound")


@pytest.fixture
async def queue(dsn: str, empty_inbound: None) -> AsyncIterator[PgmqQueue]:
    """The queue as the runtime will hold it: as `worker_role`, not as superuser.

    Reaching pgmq with the superuser would pass whatever the grants said, and
    the first thing to break in production would be a privilege the suite never
    exercised.
    """
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute("set role worker_role")
        yield PgmqQueue(conn, INBOUND)


@pytest.fixture
def queue_length(admin: psycopg.AsyncConnection) -> Callable[[], Awaitable[int]]:
    """Rows in the queue table, visible or not — the measure of "nothing in limbo"."""

    async def measure() -> int:
        cursor = await admin.execute("select queue_length from pgmq.metrics(%s)", (INBOUND,))
        row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    return measure

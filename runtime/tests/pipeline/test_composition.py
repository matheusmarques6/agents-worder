"""The composition, breathing in-process — E1 · PR 2a.

Not the scenarios yet: this is the smoke that proves the spine is connected.
A due conversation becomes an outbox row without any test code touching the
queues — coalescer, worker turn and fixed responder all real, all wired by
`app.run`, at test speed via config alone. The engine never knows.
"""

import asyncio
import time

import psycopg
import pytest

from agents_runtime.app import run
from agents_runtime.config import QueueingConfig
from tests.db.factories import create_tenant, create_thread, make_due

DEADLINE = 15


async def eventually(check, *, deadline_s: float = DEADLINE, note: str = ""):
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        result = await check()
        if result:
            return result
        await asyncio.sleep(0.05)
    raise TimeoutError(f"never became true: {note}")


@pytest.fixture
def world(sync_admin: psycopg.Connection):
    tenant_id = create_tenant(sync_admin)
    thread = create_thread(sync_admin, tenant_id)
    return tenant_id, thread


async def test_a_due_conversation_becomes_a_queued_reply(
    dsn: str, admin: psycopg.AsyncConnection, sync_admin, world, tiny_config: QueueingConfig
) -> None:
    _, thread = world
    make_due(sync_admin, thread.conversation_id, last_inbound_seq=2)

    stop = asyncio.Event()
    running = asyncio.create_task(
        run(
            dsn,
            stop=stop,
            config=tiny_config,
            worker_set_role="worker_role",
            sender_set_role="sender_role",
        )
    )
    try:

        async def replied():
            cursor = await admin.execute(
                "select status from internal.message_outbox where conversation_id = %s",
                (thread.conversation_id,),
            )
            return await cursor.fetchone()

        row = await eventually(replied, note="outbox row for the fixed reply")
        assert row[0] == "pending"  # no channel wired: queued, honestly waiting

        message = await (
            await admin.execute(
                """
                select direction, seq, author_type from public.messages
                 where conversation_id = %s and direction = 'outbound'
                """,
                (thread.conversation_id,),
            )
        ).fetchone()
        assert message == ("outbound", 1, "agent")
    finally:
        stop.set()
        await asyncio.wait_for(running, DEADLINE)


async def test_the_heartbeat_is_observable(
    dsn: str, admin: psycopg.AsyncConnection, tiny_config: QueueingConfig
) -> None:
    stop = asyncio.Event()
    running = asyncio.create_task(
        run(
            dsn,
            stop=stop,
            config=tiny_config,
            process_name="smoke-heartbeat",
            worker_set_role="worker_role",
        )
    )
    try:

        async def beat_recorded():
            cursor = await admin.execute(
                "select beat_at >= started_at from internal.runtime_heartbeats"
                " where process_name = 'smoke-heartbeat'"
            )
            return await cursor.fetchone()

        assert (await eventually(beat_recorded, note="heartbeat row"))[0] is True
    finally:
        stop.set()
        await asyncio.wait_for(running, DEADLINE)


async def test_a_stopped_composition_returns_promptly(
    dsn: str, tiny_config: QueueingConfig
) -> None:
    stop = asyncio.Event()
    stop.set()

    await asyncio.wait_for(
        run(dsn, stop=stop, config=tiny_config, worker_set_role="worker_role"),
        DEADLINE,
    )

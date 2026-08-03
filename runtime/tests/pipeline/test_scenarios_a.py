"""Cenários 1, 3 and 8 — the composition proven as an engine (E1 · PR 2b).

Scenario 1 is the whole thread at once: five messages in a burst leave the
webhook and one reply reaches the (fake) provider, with every invariant on
the way asserted by state, not by logs. Scenario 3 is redelivery answered by
validation instead of a second generation. Scenario 8 is the loop consuming
in EXACTLY the order the unit-level policy dictates — the same schedule, item
by item.
"""

import asyncio
import time

import psycopg
from psycopg.types.json import Jsonb

from agents_runtime.app import run
from agents_runtime.clock import SystemClock
from agents_runtime.config import QueueingConfig
from agents_runtime.queueing import DOMAIN_EVENTS, EVALS, INBOUND, SCHEDULED
from agents_runtime.queueing.engine_loop import Ack, EngineLoop
from agents_runtime.queueing.polling import _schedule
from agents_runtime.repository.queue import PgmqQueue
from tests.db.factories import (
    create_channel_account,
    create_tenant,
    create_thread,
    unique_id,
    unique_phone,
)
from tests.support.fake_channel import FakeChannel
from tests.support.randomness import FixedRandomness

DEADLINE = 20


async def eventually(check, *, deadline_s: float = DEADLINE, note: str = ""):
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        result = await check()
        if result:
            return result
        await asyncio.sleep(0.05)
    raise TimeoutError(f"never became true: {note}")


def ingest_message(
    sync_admin: psycopg.Connection, account_id: str, phone: str, text: str
):
    return sync_admin.execute(
        "select * from internal.ingest_webhook('meta', %s, %s, 'message_inbound', %s,"
        " interval '30 milliseconds')",
        (account_id, unique_id("evt"), Jsonb({"from": phone, "message": {"text": text}})),
    ).fetchone()


async def test_scenario_1_a_burst_of_five_becomes_exactly_one_reply(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
    tiny_config: QueueingConfig,
) -> None:
    tenant_id = create_tenant(sync_admin)
    number = create_channel_account(sync_admin, tenant_id)
    phone = unique_phone()

    # The burst lands BEFORE the engine wakes: five webhooks, one contact, one
    # conversation, deadline already expired. What the engine owes us is ONE
    # job, ONE generation, ONE reply — not five.
    for text in ("oi", "meu pedido não chegou", "alguém aí?", "??", "pode verificar?"):
        ingest_message(sync_admin, number.external_account_id, phone, text)

    stop = asyncio.Event()
    running = asyncio.create_task(
        run(
            dsn,
            stop=stop,
            config=tiny_config,
            channel=FakeChannel(dsn),
            worker_set_role="worker_role",
            sender_set_role="sender_role",
        )
    )
    try:

        async def delivered():
            cursor = await admin.execute("select count(*) from testing.fake_channel_sends")
            return (await cursor.fetchone())[0] > 0

        await eventually(delivered, note="the reply reaching the fake provider")
    finally:
        stop.set()
        await asyncio.wait_for(running, DEADLINE)

    # One job for the whole burst: exactly one archived inbound job exists.
    jobs = await (await admin.execute("select count(*) from pgmq.a_q_inbound")).fetchone()
    assert jobs[0] == 1

    # One outbound message, sequenced by the official counter.
    outbound = await (
        await admin.execute(
            "select seq, author_type from public.messages where direction = 'outbound'"
        )
    ).fetchall()
    assert outbound == [(1, "agent")]

    # The conversation is answered up to the burst's last message, lease clear.
    state = await (
        await admin.execute(
            "select last_processed_seq, next_inbound_seq, processing_token"
            " from public.conversations"
        )
    ).fetchone()
    assert state == (5, 5, None)

    # The outbox reached `sent` with the provider id the fake returned, and the
    # fake was hit EXACTLY once — one delivery, zero duplicates.
    outbox = await (
        await admin.execute("select status, provider_message_id from internal.message_outbox")
    ).fetchall()
    assert len(outbox) == 1
    assert outbox[0][0] == "sent"
    assert outbox[0][1].startswith("fake-wamid-")

    sends = await (
        await admin.execute("select count(*) from testing.fake_channel_sends")
    ).fetchone()
    assert sends[0] == 1


async def test_scenario_3_a_redelivered_job_is_archived_without_a_second_generation(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
    tiny_config: QueueingConfig,
) -> None:
    tenant_id = create_tenant(sync_admin)
    thread = create_thread(sync_admin, tenant_id)

    # A conversation already answered up to seq 3 — and then pgmq redelivers
    # the job that answered it (a VT expiry, a crash after work, any of the
    # normal reasons). Dedup is validation in the worker, not a queue feature.
    sync_admin.execute(
        "update public.conversations set next_inbound_seq = 3, last_processed_seq = 3,"
        " processing_generation = 1 where id = %s",
        (thread.conversation_id,),
    )
    sync_admin.execute(
        "select pgmq.send('q_inbound', %s)",
        (
            Jsonb(
                {
                    "conversation_id": str(thread.conversation_id),
                    "generation": 1,
                    "target_seq": 3,
                    "tenant_id": str(tenant_id),
                    "otel": None,
                }
            ),
        ),
    )

    responder_calls = 0

    async def counting_responder(job):
        nonlocal responder_calls
        responder_calls += 1
        return {"text": "não deveria acontecer"}

    stop = asyncio.Event()
    running = asyncio.create_task(
        run(
            dsn,
            stop=stop,
            config=tiny_config,
            respond=counting_responder,
            worker_set_role="worker_role",
        )
    )
    try:

        async def archived():
            cursor = await admin.execute("select count(*) from pgmq.a_q_inbound")
            return (await cursor.fetchone())[0] == 1

        await eventually(archived, note="the stale job being archived")
    finally:
        stop.set()
        await asyncio.wait_for(running, DEADLINE)

    # Archived WITHOUT work: the responder never ran, nothing was queued for
    # sending, and the conversation was left exactly as found — lease included.
    assert responder_calls == 0

    outbox = await (
        await admin.execute("select count(*) from internal.message_outbox")
    ).fetchone()
    assert outbox[0] == 0

    state = await (
        await admin.execute(
            "select last_processed_seq, processing_generation, processing_token"
            " from public.conversations where id = %s",
            (thread.conversation_id,),
        )
    ).fetchone()
    assert state == (3, 1, None)


async def test_scenario_8_the_loop_consumes_in_the_policy_order(
    dsn: str, tiny_config: QueueingConfig
) -> None:
    """One window of mixed work, consumed in the schedule's exact sequence.

    All four queues pre-filled to their weight, one loop, beliefs true
    throughout — so the consumption order IS the smooth schedule of the unit
    policy, element by element. Not "roughly 8:4:2:1": the same list.
    """
    stop = asyncio.Event()
    consumed: list[str] = []
    fills = {INBOUND: 8, DOMAIN_EVENTS: 4, SCHEDULED: 2, EVALS: 1}

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute("set role worker_role")
        queues = {name: PgmqQueue(conn, name) for name in fills}

        for name, count in fills.items():
            for index in range(count):
                await queues[name].send({"n": index})

        async def recorder(queue_name: str, message) -> Ack:
            consumed.append(queue_name)
            if len(consumed) == sum(fills.values()):
                stop.set()
            return Ack.ARCHIVE

        loop = EngineLoop(
            queues=queues,
            handlers=dict.fromkeys(fills, recorder),
            config=tiny_config,
            clock=SystemClock(),
            randomness=FixedRandomness(),
            stop=stop,
        )

        await asyncio.wait_for(loop.run(), DEADLINE)

    assert consumed == list(_schedule(tiny_config.weights))

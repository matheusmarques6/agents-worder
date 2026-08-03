"""Cenários 2, 5, 6 and 9 — steered concurrency (E1 · PR 3).

Scenario 2 is the reason the whole architecture exists in the shape it has:
a message arriving DURING generation invalidates the draft, and the customer
gets one answer covering everything they said — never a stale reply to the
question before last.

Every assertion here is about RESULTS — rows, counts, final states — never
about internal ordering between tenants or workers. That is this file's
anti-flaky rule: the engine is free to interleave however the scheduler
likes, as long as the world it leaves behind is the one the invariants
demand.
"""

import asyncio
import time

import psycopg
from psycopg.types.json import Jsonb

from agents_runtime.app import run
from agents_runtime.config import QueueingConfig, config_from_env
from agents_runtime.queueing.jobs import InboundJob
from tests.db.factories import (
    create_channel_account,
    create_tenant,
    create_thread,
    make_due,
    unique_id,
    unique_phone,
)
from tests.support.fake_channel import FakeChannel
from tests.support.holdable import (
    arm_gate,
    holdable_responder,
    holders_started,
    release_gate,
)
from tests.support.runtime_process import TINY_INTERVALS

DEADLINE = 20


async def eventually(check, *, deadline_s: float = DEADLINE, note: str = ""):
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        result = await check()
        if result:
            return result
        await asyncio.sleep(0.05)
    raise TimeoutError(f"never became true: {note}")


def ingest_message(sync_admin: psycopg.Connection, account_id: str, phone: str, text: str):
    return sync_admin.execute(
        "select * from internal.ingest_webhook('meta', %s, %s, 'message_inbound', %s,"
        " interval '30 milliseconds')",
        (account_id, unique_id("evt"), Jsonb({"from": phone, "message": {"text": text}})),
    ).fetchone()


async def test_scenario_2_a_message_during_generation_invalidates_the_draft(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
    tiny_config: QueueingConfig,
) -> None:
    """The central invariant, end to end. The most important test of the phase.

    Five messages coalesce into generation 1. While the responder is HELD
    inside FASE 2, the sixth message arrives — `next_inbound_seq` moves, the
    coalescer's snapshot goes stale. On release, the CAS refuses generation 1:
    ZERO sends from it reach the provider. Generation 2 answers all six.
    """
    tenant_id = create_tenant(sync_admin)
    number = create_channel_account(sync_admin, tenant_id)
    phone = unique_phone()

    first = ingest_message(sync_admin, number.external_account_id, phone, "oi")
    conversation_id = first[3]
    for text in ("meu pedido", "não chegou", "??", "alguém?"):
        ingest_message(sync_admin, number.external_account_id, phone, text)

    arm_gate(sync_admin, conversation_id, holds=1)

    stop = asyncio.Event()
    running = asyncio.create_task(
        run(
            dsn,
            stop=stop,
            config=tiny_config,
            respond=holdable_responder(dsn),
            channel=FakeChannel(dsn),
            worker_set_role="worker_role",
            sender_set_role="sender_role",
        )
    )
    try:
        # The turn is inside FASE 2 — a database predicate, not a guess.
        async def held():
            return holders_started(sync_admin, conversation_id) >= 1

        await eventually(held, note="generation 1 held inside FASE 2")

        # The sixth message lands while the draft is being written.
        ingest_message(sync_admin, number.external_account_id, phone, "e mais isso")
        release_gate(sync_admin, conversation_id)

        async def answered_everything():
            cursor = await admin.execute(
                "select last_processed_seq from public.conversations where id = %s",
                (conversation_id,),
            )
            return (await cursor.fetchone())[0] == 6

        await eventually(answered_everything, note="one reply covering all six")

        async def delivered():
            cursor = await admin.execute("select count(*) from testing.fake_channel_sends")
            return (await cursor.fetchone())[0] >= 1

        await eventually(delivered, note="the reply reaching the fake provider")
    finally:
        stop.set()
        await asyncio.wait_for(running, DEADLINE)

    # Zero sends from generation 1: the only outbox row belongs to generation
    # 2, and the provider was hit exactly once. The stale draft died unsent.
    outbox = await (
        await admin.execute("select idempotency_key, status from internal.message_outbox")
    ).fetchall()
    assert len(outbox) == 1
    assert outbox[0][0] == f"reply-{conversation_id}-2"
    assert outbox[0][1] == "sent"

    sends = await (
        await admin.execute("select count(*) from testing.fake_channel_sends")
    ).fetchone()
    assert sends[0] == 1

    # Two jobs lived: generation 1 (superseded, archived without effect) and
    # generation 2 (done). Both accounted for, neither in limbo.
    jobs = await (await admin.execute("select count(*) from pgmq.a_q_inbound")).fetchone()
    assert jobs[0] == 2


async def test_scenario_5_an_expired_lease_is_assumed_and_the_late_worker_sends_nothing(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
) -> None:
    """Worker 1 stalls mid-turn past its lease; worker 2 assumes and answers.

    The lease is milliseconds and the work-heartbeat is deliberately HUGE:
    a stalled renewal is the scenario — a worker whose whole process paused
    (GC, network) does not renew either. pgmq redelivers, worker 2 claims the
    expired lease, concludes; worker 1 wakes and its CAS refuses. One send.
    """
    config = config_from_env(
        {
            **TINY_INTERVALS,
            "AGENTS_LEASE_MS": "200",
            "AGENTS_VT_MS": "1000",
            "AGENTS_WORK_HEARTBEAT_MS": "60000",
        }
    )

    tenant_id = create_tenant(sync_admin)
    thread = create_thread(sync_admin, tenant_id)
    make_due(sync_admin, thread.conversation_id, last_inbound_seq=1)

    # One hold: the FIRST worker stalls, the redelivery sails through.
    arm_gate(sync_admin, thread.conversation_id, holds=1)

    stop = asyncio.Event()
    running = asyncio.create_task(
        run(
            dsn,
            stop=stop,
            config=config,
            respond=holdable_responder(dsn),
            channel=FakeChannel(dsn),
            worker_set_role="worker_role",
            sender_set_role="sender_role",
        )
    )
    try:

        async def delivered():
            cursor = await admin.execute("select count(*) from testing.fake_channel_sends")
            return (await cursor.fetchone())[0] >= 1

        await eventually(delivered, note="worker 2's reply delivered")

        # Only now does the stalled worker wake up — AFTER the conversation
        # was already answered by someone else.
        release_gate(sync_admin, thread.conversation_id)

        async def first_worker_done():
            cursor = await admin.execute(
                "select processing_token is null from public.conversations where id = %s",
                (thread.conversation_id,),
            )
            return (await cursor.fetchone())[0]

        await eventually(first_worker_done, note="the late worker finishing without effect")
    finally:
        stop.set()
        await asyncio.wait_for(running, DEADLINE)

    # One reply, not two: worker 1's draft died at the CAS.
    outbound = await (
        await admin.execute(
            "select count(*) from public.messages where direction = 'outbound'"
        )
    ).fetchone()
    assert outbound[0] == 1

    sends = await (
        await admin.execute("select count(*) from testing.fake_channel_sends")
    ).fetchone()
    assert sends[0] == 1

    # The single job was read twice — the redelivery IS the mechanism here.
    read_ct = await (
        await admin.execute("select read_ct from pgmq.a_q_inbound")
    ).fetchone()
    assert read_ct[0] >= 2


async def test_scenario_6_the_keepalive_prevents_redelivery_during_long_work(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
) -> None:
    """Renewal proved by the non-happening: a slow turn, VT tiny, read_ct 1.

    The turn is held across the VT window. Without the keepalive pushing the
    VT forward, pgmq would redeliver and read_ct would climb. With it, the
    message is read exactly once, worked once, archived once.

    The margins are chosen from measurement, not taste: this machine (Docker
    Postgres on Windows, connection churn between tests) stalls the event
    loop up to ~2s under group load, which flaked a 1.6s slack twice before
    these numbers. Slack here is 2.8s (VT 3s, beat 200ms); production runs
    15s of ABSOLUTE slack (beat 45s, VT 60s), five times this proportion.
    """
    config = config_from_env(
        {
            **TINY_INTERVALS,
            "AGENTS_VT_MS": "3000",
            "AGENTS_WORK_HEARTBEAT_MS": "200",
        }
    )

    tenant_id = create_tenant(sync_admin)
    thread = create_thread(sync_admin, tenant_id)
    make_due(sync_admin, thread.conversation_id, last_inbound_seq=1)
    arm_gate(sync_admin, thread.conversation_id, holds=1)

    stop = asyncio.Event()
    running = asyncio.create_task(
        run(
            dsn,
            stop=stop,
            config=config,
            respond=holdable_responder(dsn),
            channel=FakeChannel(dsn),
            worker_set_role="worker_role",
            sender_set_role="sender_role",
        )
    )
    try:

        async def held():
            return holders_started(sync_admin, thread.conversation_id) >= 1

        await eventually(held, note="the turn held inside FASE 2")

        # Hold across several VT windows — the redelivery that must NOT happen.
        await asyncio.sleep(4.0)
        release_gate(sync_admin, thread.conversation_id)

        # Stop only after the reply LANDS — stopping at archive raced the
        # sender's next poll and lost on CI (sends == 0) while winning
        # locally. The stop condition is the last observable, not the first.
        async def delivered():
            cursor = await admin.execute("select count(*) from testing.fake_channel_sends")
            return (await cursor.fetchone())[0] >= 1

        await eventually(delivered, note="the slow turn's reply delivered")
    finally:
        stop.set()
        await asyncio.wait_for(running, DEADLINE)

    read_ct = await (await admin.execute("select read_ct from pgmq.a_q_inbound")).fetchone()
    assert read_ct[0] == 1, "the keepalive must keep the message invisible while the work lasts"

    sends = await (
        await admin.execute("select count(*) from testing.fake_channel_sends")
    ).fetchone()
    assert sends[0] == 1


async def test_scenario_9_a_full_tenant_postpones_its_own_jobs_and_nobody_elses(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
    tiny_config: QueueingConfig,
) -> None:
    """Four jobs of tenant A against the cap of three, one job of tenant B.

    B concludes while three of A's turns are still held — the cap isolates
    tenants instead of queueing the platform behind the noisiest one. A's
    fourth comes back via set_vt and concludes later. Results asserted,
    internal order never.
    """
    tenant_a = create_tenant(sync_admin)
    tenant_b = create_tenant(sync_admin)
    threads_a = [create_thread(sync_admin, tenant_a) for _ in range(4)]
    thread_b = create_thread(sync_admin, tenant_b)
    for thread in (*threads_a, thread_b):
        make_due(sync_admin, thread.conversation_id, last_inbound_seq=1)

    a_active = 0
    a_peak = 0
    release_a = asyncio.Event()
    b_done = asyncio.Event()

    async def steering_responder(job: InboundJob):
        nonlocal a_active, a_peak
        if job.tenant_id == tenant_a:
            a_active += 1
            a_peak = max(a_peak, a_active)
            await release_a.wait()
            a_active -= 1
        else:
            b_done.set()
        return {"text": "ok"}

    stop = asyncio.Event()
    running = asyncio.create_task(
        run(
            dsn,
            stop=stop,
            config=tiny_config,
            respond=steering_responder,
            workers=5,
            worker_set_role="worker_role",
        )
    )
    try:
        # Three of A held — the cap — while B's lane stays open.
        async def a_saturated():
            return a_active == 3

        await eventually(a_saturated, note="tenant A at its cap")

        await asyncio.wait_for(b_done.wait(), DEADLINE)

        release_a.set()

        async def everything_concluded():
            cursor = await admin.execute("select count(*) from pgmq.a_q_inbound")
            return (await cursor.fetchone())[0] == 5

        await eventually(everything_concluded, note="all five jobs archived")
    finally:
        stop.set()
        release_a.set()
        await asyncio.wait_for(running, DEADLINE)

    assert a_peak == 3, f"the cap is 3; observed {a_peak} concurrent turns of tenant A"

    answered = await (
        await admin.execute(
            "select count(*) from public.conversations where last_processed_seq = 1"
        )
    ).fetchone()
    assert answered[0] == 5, "postponed is not dropped: every conversation ends answered"

"""Cenários 4, 7 and 10 — death and resurrection (E1 · PR 4, closes Fase 1).

Scenario 4 kills the REAL process and asserts that both crash windows
converge to the same final state: one reply, nothing lost, nothing doubled.
Scenario 7 walks a poisoned job to the DLQ and back — a waiting room, not a
cemetery. Scenario 10 is ADR-8 made executable: when the machine cannot know
whether a message left, it never resends; it correlates or it asks a human.

Every wait is a database predicate; every stop condition is the LAST
observable of its flow.
"""

import asyncio
import time

import psycopg
from psycopg.types.json import Jsonb

from agents_runtime.app import run
from agents_runtime.config import QueueingConfig, config_from_env
from agents_runtime.queueing import INBOUND
from agents_runtime.queueing.jobs import InboundJob
from tests.db.factories import (
    create_outbox_item,
    create_tenant,
    create_thread,
    make_due,
)
from tests.support.fake_channel import FakeChannel
from tests.support.holdable import arm_gate, holders_started
from tests.support.runtime_process import TINY_INTERVALS, RuntimeProcess, wait_until

DEADLINE = 25


async def eventually(check, *, deadline_s: float = DEADLINE, note: str = ""):
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        result = await check()
        if result:
            return result
        await asyncio.sleep(0.05)
    raise TimeoutError(f"never became true: {note}")


def q(dsn: str, statement: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(statement, params).fetchall()


# --- cenário 4 · janela (a): morte no meio da FASE 2 --------------------------


def test_scenario_4a_a_kill_mid_turn_loses_nothing_and_doubles_nothing(
    dsn: str, sync_admin: psycopg.Connection
) -> None:
    """SIGKILL with the lease held and the draft unwritten.

    The dead worker's lease expires, pgmq redelivers, the resurrected process
    concludes. The customer sees exactly one reply and no trace of the death.
    """
    tenant_id = create_tenant(sync_admin)
    thread = create_thread(sync_admin, tenant_id)
    make_due(sync_admin, thread.conversation_id, last_inbound_seq=1)
    arm_gate(sync_admin, thread.conversation_id, holds=1)

    lease_env = {
        "AGENTS_LEASE_MS": "1000",
        "AGENTS_VT_MS": "1000",
        "AGENTS_RESPONDER": "tests.support.holdable:holdable_responder",
    }

    with RuntimeProcess(dsn, name="c4a-first", extra_env=lease_env) as first:
        # Mid-FASE-2, provable: the gate counted a holder AND the lease is set.
        wait_until(
            lambda: holders_started(sync_admin, thread.conversation_id) >= 1,
            note="the turn held inside FASE 2",
        )
        wait_until(
            lambda: q(
                dsn,
                "select processing_token is not null from public.conversations where id = %s",
                (thread.conversation_id,),
            )[0][0],
            note="the lease held by the doomed worker",
        )

        first.kill()

    # Resurrection: the gate's single hold is spent, so the redelivered job
    # sails straight through to conclusion and delivery. The wait is on the
    # LAST observables — outbox `sent` and the job archived — never on the
    # fake's delivery record: the fake records BEFORE the sender's mark_sent,
    # and this harness's teardown on Windows is a hard kill, so leaving on
    # delivery raced the mark and left `sending` behind (decisão 61, again).
    with RuntimeProcess(dsn, name="c4a-second", extra_env=lease_env):
        wait_until(
            lambda: q(
                dsn,
                "select (select count(*) from internal.message_outbox where status = 'sent')"
                " + (select count(*) from pgmq.a_q_inbound)",
            )[0][0]
            == 2,
            note="the reply sent and the job archived",
        )

    # Convergence: one outbound message, one sent outbox row, job archived
    # after at least two reads (the redelivery IS the resurrection path).
    assert q(dsn, "select count(*) from public.messages where direction = 'outbound'")[0][0] == 1
    assert q(dsn, "select status from internal.message_outbox")[0][0] == "sent"
    assert q(dsn, "select count(*) from pgmq.a_q_inbound")[0][0] == 1
    assert q(dsn, "select read_ct from pgmq.a_q_inbound")[0][0] >= 2
    assert (
        q(
            dsn,
            "select last_processed_seq, processing_token from public.conversations where id = %s",
            (thread.conversation_id,),
        )[0]
        == (1, None)
    )


# --- cenário 4 · janela (b): morte entre a conclusão e o archive --------------


async def test_scenario_4b_a_crash_between_conclusion_and_archive_converges(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
    tiny_config: QueueingConfig,
) -> None:
    """The nastier window, constructed rather than raced.

    Between conclude_turn committing and queue.archive, the window is
    microseconds wide — no seam can hold a real process there
    deterministically. So the test builds the EXACT post-crash state the
    window leaves behind: conclusion committed (reply row, outbox row,
    counters advanced) and the job still in the queue. What matters is the
    convergence: redelivery finds STALE, archives without a second turn, and
    the pending send goes out once.
    """
    tenant_id = create_tenant(sync_admin)
    thread = create_thread(sync_admin, tenant_id)

    sync_admin.execute(
        "update public.conversations set next_inbound_seq = 3, last_processed_seq = 3,"
        " processing_generation = 1, version = 1 where id = %s",
        (thread.conversation_id,),
    )
    sync_admin.execute(
        """
        insert into public.messages
            (tenant_id, conversation_id, direction, seq, channel, author_type, content)
        values (%s, %s, 'outbound', 1, 'whatsapp_cloud', 'agent', '{"text": "resposta"}'::jsonb)
        """,
        (tenant_id, thread.conversation_id),
    )
    outbox_id = create_outbox_item(sync_admin, tenant_id, thread)
    sync_admin.execute(
        "select pgmq.send(%s, %s)",
        (
            INBOUND,
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

    async def counting_responder(job: InboundJob):
        nonlocal responder_calls
        responder_calls += 1
        return {"text": "segunda resposta que não deve existir"}

    stop = asyncio.Event()
    running = asyncio.create_task(
        run(
            dsn,
            stop=stop,
            config=tiny_config,
            respond=counting_responder,
            channel=FakeChannel(dsn),
            worker_set_role="worker_role",
            sender_set_role="sender_role",
        )
    )
    try:

        async def delivered_and_archived():
            sends = await (
                await admin.execute("select count(*) from testing.fake_channel_sends")
            ).fetchone()
            archived = await (
                await admin.execute("select count(*) from pgmq.a_q_inbound")
            ).fetchone()
            return sends[0] == 1 and archived[0] == 1

        await eventually(delivered_and_archived, note="the crash aftermath converging")
    finally:
        stop.set()
        await asyncio.wait_for(running, DEADLINE)

    # No second turn happened: the responder never ran, no second outbound row,
    # no second outbox entry — the redelivered job died as STALE.
    assert responder_calls == 0
    assert (
        await (
            await admin.execute(
                "select count(*) from public.messages where direction = 'outbound'"
            )
        ).fetchone()
    )[0] == 1

    status = await (
        await admin.execute(
            "select status from internal.message_outbox where id = %s", (outbox_id,)
        )
    ).fetchone()
    assert status[0] == "sent"


# --- cenário 7 · envenenada, ida e volta ---------------------------------------


async def test_scenario_7_a_poisoned_job_goes_to_the_dlq_and_comes_back(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
    tiny_config: QueueingConfig,
) -> None:
    """Permanent failure → the right DLQ, payload intact → manual reprocess →
    the same job concludes. A waiting room, not a cemetery."""
    tenant_id = create_tenant(sync_admin)
    thread = create_thread(sync_admin, tenant_id)
    make_due(sync_admin, thread.conversation_id, last_inbound_seq=1)

    poisoned = True

    async def curable_responder(job: InboundJob):
        if poisoned:
            raise ValueError("payload inválido: veneno de teste")
        return {"text": "curado"}

    stop = asyncio.Event()
    running = asyncio.create_task(
        run(
            dsn,
            stop=stop,
            config=tiny_config,
            respond=curable_responder,
            worker_set_role="worker_role",
        )
    )
    try:

        async def dead_lettered():
            cursor = await admin.execute("select queue_length from pgmq.metrics('q_inbound_dlq')")
            return (await cursor.fetchone())[0] == 1

        await eventually(dead_lettered, note="the poisoned job reaching its own DLQ")

        # Forensics travel with the corpse; the original payload stays whole.
        dead = await (
            await admin.execute("select message from pgmq.q_q_inbound_dlq")
        ).fetchone()
        assert dead[0]["error_class"] == "ValueError"
        assert dead[0]["conversation_id"] == str(thread.conversation_id)

        # The cure, then the way back: reprocess strips the forensics and the
        # job returns to its origin as if fresh.
        poisoned = False
        moved = await (
            await admin.execute(
                "select internal.reprocess_dead_letters('q_inbound_dlq', 'q_inbound')"
            )
        ).fetchone()
        assert moved[0] == 1

        async def concluded():
            cursor = await admin.execute(
                "select last_processed_seq from public.conversations where id = %s",
                (thread.conversation_id,),
            )
            return (await cursor.fetchone())[0] == 1

        await eventually(concluded, note="the reprocessed job concluding")
    finally:
        stop.set()
        await asyncio.wait_for(running, DEADLINE)

    assert (
        await (await admin.execute("select queue_length from pgmq.metrics('q_inbound_dlq')"))
        .fetchone()
    )[0] == 0

    replayed = await (
        await admin.execute("select message from pgmq.a_q_inbound order by msg_id desc limit 1")
    ).fetchone()
    assert "error_class" not in replayed[0], "the returning job must look fresh"


async def test_scenario_7_exhaustion_also_ends_in_the_dlq(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
) -> None:
    """The transient that never heals: five retries, then the DLQ — the limit
    is what keeps 'transient' from meaning 'forever'."""
    config = config_from_env(
        {
            **TINY_INTERVALS,
            "AGENTS_BACKOFF_BASE_MS": "100",
            "AGENTS_BACKOFF_CAP_MS": "1000",
        }
    )
    tenant_id = create_tenant(sync_admin)
    thread = create_thread(sync_admin, tenant_id)
    make_due(sync_admin, thread.conversation_id, last_inbound_seq=1)

    async def always_flaky(job: InboundJob):
        raise ConnectionError("HTTP 503 sempre")

    stop = asyncio.Event()
    running = asyncio.create_task(
        run(dsn, stop=stop, config=config, respond=always_flaky, worker_set_role="worker_role")
    )
    try:

        async def exhausted():
            cursor = await admin.execute("select queue_length from pgmq.metrics('q_inbound_dlq')")
            return (await cursor.fetchone())[0] == 1

        await eventually(exhausted, note="the retry limit routing to the DLQ")
    finally:
        stop.set()
        await asyncio.wait_for(running, DEADLINE)

    dead = await (await admin.execute("select read_ct from pgmq.a_q_inbound")).fetchone()
    assert dead[0] == 6, "five retries allowed, the sixth read is the one that gives up"


# --- cenário 10 · ADR-8, o unknown sem reenvio cego ----------------------------


def test_scenario_10_a_dead_sender_never_causes_a_resend(
    dsn: str, sync_admin: psycopg.Connection
) -> None:
    """The provider HAS the message; our process died before learning it.

    The resurrected sender sweeps the orphaned 'sending' to unknown and — the
    central assertion of ADR-8 — the fake still holds exactly ONE record.
    Correlation (the status webhook echoing biz_opaque_callback_data, which IS
    the idempotency_key) is what resolves it to sent. Never a resend.
    """
    tenant_id = create_tenant(sync_admin)
    thread = create_thread(sync_admin, tenant_id)
    outbox_id = create_outbox_item(sync_admin, tenant_id, thread)
    key = q(dsn, "select idempotency_key from internal.message_outbox where id = %s", (outbox_id,))[
        0
    ][0]

    # The directive: record the send, then hold until killed.
    sync_admin.execute(
        "insert into testing.fake_channel_directives (idempotency_key, behavior)"
        " values (%s, 'hold_after_send')",
        (key,),
    )

    env = {"AGENTS_SEND_LEASE_MS": "500"}

    with RuntimeProcess(dsn, name="c10-first", extra_env=env) as first:
        wait_until(
            lambda: q(dsn, "select count(*) from testing.fake_channel_sends")[0][0] == 1
            and q(dsn, "select status from internal.message_outbox where id = %s", (outbox_id,))[0][
                0
            ]
            == "sending",
            note="the send recorded at the provider, our record still in flight",
        )

        first.kill()

    with RuntimeProcess(dsn, name="c10-second", extra_env=env):
        wait_until(
            lambda: q(
                dsn,
                "select status from internal.message_outbox where id = %s",
                (outbox_id,),
            )[0][0]
            == "unknown",
            note="the sweep turning the orphan into unknown",
        )

        # Several sender polls later, the count MUST still be one — this line
        # is the ADR: on doubt, never resend.
        time.sleep(0.6)
        assert q(dsn, "select count(*) from testing.fake_channel_sends")[0][0] == 1

        # The evidence arrives: the status webhook echoes the key.
        sync_admin.execute("set role ingestion_role")
        sync_admin.execute(
            "select internal.correlate_outbox_status(%s, 'sent', 'wamid.real')", (key,)
        )
        sync_admin.execute("reset role")

        wait_until(
            lambda: q(
                dsn,
                "select status, provider_message_id from internal.message_outbox where id = %s",
                (outbox_id,),
            )[0]
            == ("sent", "wamid.real"),
            note="correlation resolving the unknown",
        )

    assert q(dsn, "select count(*) from testing.fake_channel_sends")[0][0] == 1


async def test_scenario_10_variant_no_evidence_ends_in_manual_review(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
) -> None:
    """The window closes with no webhook: a human is asked. Still no resend."""
    config = config_from_env(
        {**TINY_INTERVALS, "AGENTS_SEND_LEASE_MS": "300", "AGENTS_REVIEW_MS": "500"}
    )
    tenant_id = create_tenant(sync_admin)
    thread = create_thread(sync_admin, tenant_id)
    outbox_id = create_outbox_item(sync_admin, tenant_id, thread, status="sending")
    sync_admin.execute(
        "update internal.message_outbox set locked_by = 'dead-sender',"
        " locked_until = now() - interval '1 second', request_started_at = now()"
        " where id = %s",
        (outbox_id,),
    )

    stop = asyncio.Event()
    running = asyncio.create_task(
        run(
            dsn,
            stop=stop,
            config=config,
            channel=FakeChannel(dsn),
            worker_set_role="worker_role",
            sender_set_role="sender_role",
        )
    )
    try:

        async def under_review():
            cursor = await admin.execute(
                "select status from internal.message_outbox where id = %s", (outbox_id,)
            )
            return (await cursor.fetchone())[0] == "manual_review"

        await eventually(under_review, note="the review window closing on the unknown")
    finally:
        stop.set()
        await asyncio.wait_for(running, DEADLINE)

    sends = await (
        await admin.execute("select count(*) from testing.fake_channel_sends")
    ).fetchone()
    assert sends[0] == 0, "an unknown is never resent — not even on its way to a human"

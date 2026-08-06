"""A corrida do D2, encenada contra o handler inteiro.

`tests/db/test_dispatch_touch_cas.py` stages the same race against the SQL. This
file stages it one level up, against `run_touch` — the whole three-phase shape,
with the real ladder in the middle:

    FASE 1  load the snapshot (short transaction, then it closes)
    FASE 2  the ladder decides — allow — in Python, outside every transaction
    FASE 3  the compare-and-set

and the fact is injected in the gap that D2 exists for: after the ladder said
yes, before the write landed. The mechanism is a lock, not a patch (see
`tests/support/lock_race.py`): a spectator holds the touch's row, so the write
blocks with the decision already made, and the injection commits while it waits.

What has to come out of it is not only "nothing was sent" but the RIGHT WORD for
why. `internal.dispatch_touch` returns `conflict` and refuses to name a reason —
naming one in SQL would be the ladder written a second time, precedence and all.
So the handler reloads the facts and runs the ladder AGAIN, and it is that
second decision the touch is cancelled with: `stale_newer_message`, the ladder's
own vocabulary, never a synonym (D7 — and the S2 finding that two words for one
fact break the metric S11 asks for).
"""

import asyncio
import uuid

import psycopg
import pytest

from agents_runtime.clock import SystemClock
from agents_runtime.queueing.dispatcher import run_touch
from agents_runtime.queueing.engine_loop import Ack
from agents_runtime.queueing.jobs import ScheduledTouchJob
from tests.db.factories import create_tenant, create_thread
from tests.db.factories_e3 import create_funnel, create_scheduled_touch
from tests.support.database import as_runtime_worker
from tests.support.lock_race import wait_until_blocked

CADENCE = [{"n": 1, "delay": "PT0S", "copy_base": "Vi que ficou algo no carrinho."}]


@pytest.fixture
def world(sync_admin: psycopg.Connection):
    tenant_id = create_tenant(sync_admin)
    thread = create_thread(sync_admin, tenant_id)
    funnel = create_funnel(sync_admin, tenant_id, touches=CADENCE)
    touch_id = create_scheduled_touch(
        sync_admin,
        tenant_id,
        funnel.id,
        thread.contact_id,
        conversation_id=thread.conversation_id,
        due_in_seconds=-60,
        event_age_seconds=60,
        status="enqueued",
    )
    return tenant_id, thread, touch_id


def _state(conn: psycopg.Connection, touch_id: uuid.UUID) -> tuple:
    return conn.execute(
        """
        select t.status, t.cancel_reason, t.sent_at, t.outbox_id,
               (select count(*) from internal.message_outbox),
               (select count(*) from public.messages where direction = 'outbound'),
               (select count(*) from testing.fake_channel_sends)
          from public.scheduled_touches t where t.id = %s
        """,
        (touch_id,),
    ).fetchone()


async def test_a_message_arriving_between_the_decision_and_the_write_kills_the_touch(
    dsn: str, sync_admin: psycopg.Connection, world
) -> None:
    tenant_id, thread, touch_id = world
    job = ScheduledTouchJob(scheduled_touch_id=touch_id, tenant_id=tenant_id)

    holder = await psycopg.AsyncConnection.connect(dsn)
    watcher = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        # The spectator takes the touch's row and keeps it. The snapshot load is
        # a plain read and sails past; the write cannot.
        await holder.execute(
            "select id from public.scheduled_touches where id = %s for update", (touch_id,)
        )

        async with as_runtime_worker(dsn) as conn:
            handling = asyncio.ensure_future(run_touch(conn, job, clock=SystemClock()))

            # The server's own view of itself: a backend is waiting on a lock
            # inside `dispatch_touch`. The ladder has already allowed the touch.
            await wait_until_blocked(watcher, needle="dispatch_touch")

            # The contact writes — the counter and the row together, exactly as
            # ingestion does it.
            await holder.execute(
                "update public.conversations set next_inbound_seq = next_inbound_seq + 1"
                " where id = %s",
                (thread.conversation_id,),
            )
            await holder.execute(
                """
                insert into public.messages
                    (tenant_id, conversation_id, direction, seq, channel, author_type, content)
                values (%s, %s, 'inbound', 1, 'whatsapp_cloud', 'contact', '{"text": "já comprei"}')
                """,
                (tenant_id, thread.conversation_id),
            )
            await holder.commit()

            ack = await asyncio.wait_for(handling, 20)
    finally:
        await holder.close()
        await watcher.close()

    assert ack is Ack.ARCHIVE

    status, reason, sent_at, outbox_id, sends, outbound, delivered = _state(sync_admin, touch_id)
    assert (status, reason) == ("cancelled", "stale_newer_message")
    assert (sent_at, outbox_id) == (None, None)
    # Nothing was written anywhere: no send, no history, no delivery.
    assert (sends, outbound, delivered) == (0, 0, 0)


async def test_a_touch_that_goes_out_in_the_meantime_spends_the_daily_allowance(
    dsn: str, sync_admin: psycopg.Connection, world
) -> None:
    """The same instant, a different fact — and the reason changes with it.

    This one is the count rather than the boolean: the ladder allowed the touch
    because the contact had received nothing in 24h, and by the time the write
    ran that was no longer true."""
    tenant_id, thread, touch_id = world
    job = ScheduledTouchJob(scheduled_touch_id=touch_id, tenant_id=tenant_id)

    holder = await psycopg.AsyncConnection.connect(dsn)
    watcher = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        await holder.execute(
            "select id from public.scheduled_touches where id = %s for update", (touch_id,)
        )

        async with as_runtime_worker(dsn) as conn:
            handling = asyncio.ensure_future(run_touch(conn, job, clock=SystemClock()))
            await wait_until_blocked(watcher, needle="dispatch_touch")

            await holder.execute(
                """
                insert into internal.message_outbox
                    (tenant_id, conversation_id, contact_id, channel_account_id,
                     kind, payload, idempotency_key)
                values (%s, %s, %s, %s, 'funnel_touch', '{"text": "outro"}', %s)
                """,
                (
                    tenant_id,
                    thread.conversation_id,
                    thread.contact_id,
                    thread.channel_account_id,
                    uuid.uuid4().hex,
                ),
            )
            await holder.commit()

            ack = await asyncio.wait_for(handling, 20)
    finally:
        await holder.close()
        await watcher.close()

    assert ack is Ack.ARCHIVE

    status, reason, _, _, sends, outbound, delivered = _state(sync_admin, touch_id)
    assert (status, reason) == ("cancelled", "rate_limit_24h")
    # The one outbox row is the injected one — ours was never written.
    assert (sends, outbound, delivered) == (1, 0, 0)


async def test_the_same_wait_with_nothing_injected_still_sends(
    dsn: str, sync_admin: psycopg.Connection, world
) -> None:
    """The control. Same lock, same block, same wakeup, no injection — and the
    touch goes out. Without it, both refusals above could be an artefact of
    having been blocked at all."""
    tenant_id, _thread, touch_id = world
    job = ScheduledTouchJob(scheduled_touch_id=touch_id, tenant_id=tenant_id)

    holder = await psycopg.AsyncConnection.connect(dsn)
    watcher = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        await holder.execute(
            "select id from public.scheduled_touches where id = %s for update", (touch_id,)
        )

        async with as_runtime_worker(dsn) as conn:
            handling = asyncio.ensure_future(run_touch(conn, job, clock=SystemClock()))
            await wait_until_blocked(watcher, needle="dispatch_touch")
            await holder.commit()

            ack = await asyncio.wait_for(handling, 20)
    finally:
        await holder.close()
        await watcher.close()

    assert ack is Ack.ARCHIVE

    status, reason, sent_at, outbox_id, sends, outbound, _ = _state(sync_admin, touch_id)
    assert (status, reason) == ("sent", None)
    assert sent_at is not None and outbox_id is not None
    assert (sends, outbound) == (1, 1)

"""A prova 1 em laboratório — o abandono atravessando o motor inteiro.

O mesmo fio que a demo real percorre, com o canal falso no lugar da Meta: o
webhook de plataforma entra pela ingestão, vira job em `q_domain_events`, o
worker aplica o toque, o sender entrega. O que a demo acrescenta a isto é só
credencial — nenhum código novo.

Condição de parada na doutrina da decisão 61: os últimos observáveis do fluxo
(outbox `sent` E job arquivado), nunca um estado do meio.
"""

import asyncio

import psycopg
from psycopg.types.json import Jsonb

from agents_runtime.agent_core.responder import FIXED_TOUCH
from agents_runtime.app import run
from agents_runtime.config import QueueingConfig
from tests.db.factories import (
    create_channel_account,
    create_connector_account,
    create_tenant,
    unique_id,
    unique_phone,
)
from tests.pipeline.test_scenarios_a import DEADLINE, eventually
from tests.support.fake_channel import FakeChannel


async def test_an_abandonment_reaches_the_fake_whatsapp(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
    tiny_config: QueueingConfig,
) -> None:
    tenant_id = create_tenant(sync_admin)
    store = create_connector_account(sync_admin, tenant_id)
    create_channel_account(sync_admin, tenant_id)
    phone = unique_phone()

    # The abandonment lands BEFORE the engine wakes — exactly how the demo
    # will run: ingest first, engine picks it up whenever it starts.
    outcome = sync_admin.execute(
        "select * from internal.ingest_webhook('shopify', %s, %s, 'checkout_abandoned', %s)",
        (store.source_account_id, unique_id("evt"), Jsonb({"phone": phone})),
    ).fetchone()
    assert outcome[0] == "ingested"
    event_id = outcome[1]

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

        async def concluded():
            cursor = await admin.execute(
                "select"
                " (select count(*) from internal.message_outbox where status = 'sent')"
                " + (select count(*) from pgmq.a_q_domain_events)"
            )
            return (await cursor.fetchone())[0] == 2

        await eventually(concluded, note="the touch sent and the job archived")
    finally:
        stop.set()
        await asyncio.wait_for(running, DEADLINE)

    # The fake provider got EXACTLY one touch, fixed text, right phone.
    sends = await (
        await admin.execute(
            "select to_phone_e164, payload, idempotency_key from testing.fake_channel_sends"
        )
    ).fetchall()
    assert sends == [(phone, {"text": FIXED_TOUCH}, f"touch-{event_id}")]

    # The outbox row is the funnel_touch, concluded with the fake's wamid.
    outbox = await (
        await admin.execute("select kind, status, provider_message_id from internal.message_outbox")
    ).fetchall()
    assert outbox == [("funnel_touch", "sent", f"fake-wamid-touch-{event_id}")]

    # The conversation the touch opened carries the occasion, and the message
    # is sequenced by the official counter.
    thread = await (
        await admin.execute(
            "select c.origin_occasion, m.direction, m.seq, m.author_type"
            " from public.conversations c join public.messages m on m.conversation_id = c.id"
        )
    ).fetchall()
    assert thread == [("checkout_abandoned", "outbound", 1, "agent")]

    # And the event closed its loop: processed, with nothing left in limbo.
    event = await (
        await admin.execute("select status from internal.webhook_events where id = %s", (event_id,))
    ).fetchone()
    assert event == ("processed",)

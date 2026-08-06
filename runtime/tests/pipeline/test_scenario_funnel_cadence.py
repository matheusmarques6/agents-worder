"""O abandono atravessando o motor inteiro — e parando antes do WhatsApp.

Sucessor de `test_scenario_abandonment.py`, que provava a prova 1 do E1: o
webhook de plataforma entrava pela ingestão, virava job em `q_domain_events`, o
worker aplicava o toque de texto fixo e o sender entregava. O E3 S3 aposentou
esse toque (D6) e, com ele, o único caminho em que um proativo saía sem passar
pela escada.

O que este arquivo prova é a **D11** de ponta a ponta, com o motor de verdade:
o mesmo evento agora materializa a cadência inteira em `scheduled_touches` e
**nada** chega ao canal falso. O RF-033 manda checar supressão antes de TODO
envio proativo e o RF-034 soma TODAS as origens na janela de 24h; enquanto o
dispatcher do S4 não existir, o comportamento correto do produto é exatamente
este — o funil fica agendado e ninguém é incomodado.

Condição de parada na doutrina da decisão 61: os últimos observáveis do fluxo
(a cadência gravada E o job arquivado), nunca um estado do meio.
"""

import asyncio

import psycopg
from psycopg.types.json import Jsonb

from agents_runtime.app import run
from agents_runtime.config import QueueingConfig
from tests.db.factories import (
    create_channel_account,
    create_connector_account,
    create_tenant,
    unique_id,
    unique_phone,
)
from tests.db.factories_e3 import create_funnel
from tests.pipeline.test_scenarios_a import DEADLINE, eventually
from tests.support.fake_channel import FakeChannel

CADENCE = [
    {"n": 1, "delay": "PT0S", "copy_base": "Vi que ficou algo no carrinho."},
    {"n": 2, "delay": "PT6H", "copy_base": "Ainda dá tempo."},
]


async def test_an_abandonment_becomes_a_cadence_and_reaches_nobody(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
    tiny_config: QueueingConfig,
) -> None:
    tenant_id = create_tenant(sync_admin)
    store = create_connector_account(sync_admin, tenant_id)
    create_channel_account(sync_admin, tenant_id)
    funnel = create_funnel(sync_admin, tenant_id, touches=CADENCE)
    phone = unique_phone()

    # O abandono cai ANTES do motor acordar — exatamente como a demo roda.
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
                " (select count(*) from public.scheduled_touches)"
                " + (select count(*) from pgmq.a_q_domain_events)"
            )
            return (await cursor.fetchone())[0] == 3

        await eventually(concluded, note="the cadence scheduled and the job archived")
    finally:
        stop.set()
        await asyncio.wait_for(running, DEADLINE)

    # A cadência do funil, ancorada no instante do evento, com o nº 1 vencido.
    touches = await (
        await admin.execute(
            """
            select t.touch_number, t.status, t.due_at <= now(), t.outbox_id, t.sent_at,
                   t.event_at = e.received_at, t.funnel_id, t.conversation_id is not null
              from public.scheduled_touches t
              join internal.webhook_events e on e.id = %s
             order by t.touch_number
            """,
            (event_id,),
        )
    ).fetchall()
    assert touches == [
        (1, "pending", True, None, None, True, funnel.id, True),
        (2, "pending", False, None, None, True, funnel.id, True),
    ]

    # D11, o coração do passo: o motor rodou inteiro e o canal não foi chamado.
    # Nem outbox, nem mensagem, nem envio — a escada do S4 é a única porta.
    silence = await (
        await admin.execute(
            "select"
            " (select count(*) from testing.fake_channel_sends)"
            " + (select count(*) from internal.message_outbox)"
            " + (select count(*) from public.messages)"
        )
    ).fetchone()
    assert silence == (0,)

    # E o evento fechou o laço: processado, sem nada em limbo.
    event = await (
        await admin.execute("select status from internal.webhook_events where id = %s", (event_id,))
    ).fetchone()
    assert event == ("processed",)

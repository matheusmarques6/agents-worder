"""O pagamento contra o motor rodando — o funil morre, e a receita fica.

O `test_scenario_funnel_dispatch` do S4 provou o fio inteiro até o canal. Este
prova o que acontece quando o contato **paga**, com a composição real (`app.run`),
os papéis de produção (`worker_role`, `sender_role`) e o único dublê que existe
por não haver número de telefone: o canal.

Dois cenários, e a diferença entre eles é a ordem em que os dois fatos chegam —
que é exatamente onde mora o risco:

  * **o pagamento chega DEPOIS do primeiro toque.** O funil já falou; o handler
    de domínio cancela na hora os toques restantes (D7) e credita a conversão
    (D8). É o cenário 11 do `core/testes-e-cicd.md` §3.3 na parte "pagamento
    cancela o funil";

  * **o pagamento chega ANTES de existir funil.** Não há toque a cancelar e não
    há contato para creditar — o efeito do evento é só o espelho. O que protege
    o contato é o pedido já estar `paid` quando a cadência nasce: a escada lê
    isso no snapshot e o primeiro toque morre com `stale_order_paid` em vez de
    sair. É o caso que a decisão 58 (expiração da crença) consertou na fila e
    que ganha aqui o teste de produto.

A condição de parada segue a decisão 61: o último observável do fluxo — o canal
com a mensagem, ou o toque cancelado —, nunca um estado intermediário.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta

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


def an_order(external_id: str, *, total: str = "199.90") -> dict:
    return {
        "external_id": external_id,
        "total": total,
        "currency": "BRL",
        "items": [{"sku": "A1", "qty": 1}],
        "customer": {"external_id": f"cust-{external_id}", "name": "Ana"},
    }


def ingest(
    sync_admin: psycopg.Connection, store_account: str, event_type: str, payload: dict
) -> tuple:
    return sync_admin.execute(
        "select * from internal.ingest_webhook('shopify', %s, %s, %s, %s)",
        (store_account, unique_id("evt"), event_type, Jsonb(payload)),
    ).fetchone()


async def test_a_payment_cancels_the_rest_of_the_funnel_and_credits_the_conversion(
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
    external_id = unique_id("ord")

    ingest(
        sync_admin,
        store.source_account_id,
        "checkout_abandoned",
        {"phone": phone, "order": an_order(external_id)},
    )

    config = replace(tiny_config, dispatcher_tick=timedelta(milliseconds=50))
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

        async def delivered():
            cursor = await admin.execute("select payload from testing.fake_channel_sends")
            return await cursor.fetchone()

        await eventually(delivered, note="the first touch reaching the channel")

        # Só AGORA o contato paga. O toque nº 2 está a seis horas de distância e
        # `pending` — é ele que o pagamento tem de matar antes que vença.
        ingest(
            sync_admin,
            store.source_account_id,
            "order_paid",
            {"phone": phone, "order": an_order(external_id, total="199.90")},
        )

        async def credited():
            cursor = await admin.execute(
                "select amount, currency, funnel_id, scheduled_touch_id"
                "  from public.funnel_conversions"
            )
            return await cursor.fetchone()

        conversion = await eventually(credited, note="the conversion being credited")
    finally:
        stop.set()
        await asyncio.wait_for(running, DEADLINE)

    touches = await (
        await admin.execute(
            """
            select touch_number, status, cancel_reason, id
              from public.scheduled_touches
             where funnel_id = %s
             order by touch_number
            """,
            (funnel.id,),
        )
    ).fetchall()
    # O nº 1 saiu e continua saído — cancelá-lo o tiraria da janela de 72h e da
    # conversão que ele mesmo ganhou. O nº 2 morre com a palavra da escada.
    assert [row[:3] for row in touches] == [
        (1, "sent", None),
        (2, "cancelled", "stale_order_paid"),
    ]

    # A conversão aponta para o toque que de fato falou, com o valor copiado do
    # espelho — nunca juntado depois, porque nem a mensagem nem o contato
    # sobrevivem à purga.
    assert (str(conversion[0]), conversion[1], conversion[2], conversion[3]) == (
        "199.90",
        "BRL",
        funnel.id,
        touches[0][3],
    )

    # E o canal ouviu uma vez só: o pagamento não fez ninguém falar.
    sent = await (await admin.execute("select count(*) from testing.fake_channel_sends")).fetchone()
    assert sent == (1,)

    # O espelho ficou pago, e o pedido sabe de quem é.
    order = await (
        await admin.execute("select financial_status, contact_id is not null from public.orders")
    ).fetchone()
    assert order == ("paid", True)


async def test_a_payment_that_arrives_before_the_funnel_never_reaches_the_contact(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
    tiny_config: QueueingConfig,
) -> None:
    """O pagamento chega ANTES de qualquer toque — e antes do próprio contato.

    Nada é cancelado, porque não há o que cancelar, e nada é creditado, porque
    ninguém falou com essa pessoa. O que o passo entrega aqui é o **alvo da
    guarda**: o abandono que chega depois carrega o mesmo pedido, a cadência
    nasce apontando para ele, e a escada recusa o primeiro toque com a palavra
    certa. A defesa em profundidade da D7 é isto — o cancelamento imediato cobre
    a ordem comum, e a revalidação no disparo cobre a invertida.
    """
    tenant_id = create_tenant(sync_admin)
    store = create_connector_account(sync_admin, tenant_id)
    create_channel_account(sync_admin, tenant_id)
    create_funnel(sync_admin, tenant_id, touches=CADENCE)
    phone = unique_phone()
    external_id = unique_id("ord")

    ingest(
        sync_admin,
        store.source_account_id,
        "order_paid",
        {"phone": phone, "order": an_order(external_id)},
    )

    config = replace(tiny_config, dispatcher_tick=timedelta(milliseconds=50))
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

        async def mirrored():
            cursor = await admin.execute(
                "select financial_status, contact_id from public.orders where external_id = %s",
                (external_id,),
            )
            return await cursor.fetchone()

        order = await eventually(mirrored, note="the payment reaching the mirror")
        # Ninguém foi criado: um pagamento não é permissão para conversar.
        assert order == ("paid", None)

        ingest(
            sync_admin,
            store.source_account_id,
            "checkout_abandoned",
            {"phone": phone, "order": an_order(external_id)},
        )

        async def cancelled():
            cursor = await admin.execute(
                "select touch_number, cancel_reason from public.scheduled_touches"
                " where status = 'cancelled'"
            )
            return await cursor.fetchone()

        first = await eventually(cancelled, note="the first touch refused by the ladder")
    finally:
        stop.set()
        await asyncio.wait_for(running, DEADLINE)

    assert first == (1, "stale_order_paid")

    silence = await (
        await admin.execute(
            "select (select count(*) from testing.fake_channel_sends)"
            " + (select count(*) from internal.message_outbox)"
            " + (select count(*) from public.funnel_conversions)"
        )
    ).fetchone()
    assert silence == (0,)

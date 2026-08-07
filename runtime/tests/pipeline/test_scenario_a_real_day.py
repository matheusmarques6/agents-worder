"""Um dia inteiro da loja, num processo só — a prova nº 1 do S12, escrita aqui.

Todos os outros cenários de `pipeline` isolam UMA coisa. Este não isola nada: é
uma loja com três contatos vivos ao mesmo tempo, no mesmo tenant, no mesmo funil,
contra a composição real (`app.run`), com os papéis de produção e um único dublê
— o canal, porque não há telefone.

Os três desfecham DIFERENTE, e é por isso que estão juntos:

  * **Ana responde.** Ela recebe o toque, escreve de volta perguntando outra
    coisa, o agente responde (a conversa virou suporte comum), e o toque nº 2 do
    funil morre quando vence — `stale_newer_message`. Depois ela paga, dentro da
    janela, e a conversão é creditada ao toque que de fato falou com ela;
  * **Bruno bloqueia.** Ele recebe o mesmo toque, com os mesmos dois botões, e
    toca em *Bloquear*. O motor — não o modelo — reconhece o id que nós emitimos
    e o inscreve na `suppression_list`. O toque nº 2 dele morre com
    `suppressed_block`;
  * **Carla paga.** O toque nº 2 dela nem chega a vencer: o pagamento cancela na
    hora, `stale_order_paid`, e credita a conversão.

A diferença entre os três é exatamente onde o produto vive, e o teste existe
para afirmar que ela é **por contato**: nada do que aconteceu com um mudou o
desfecho dos outros. Um bug de escopo — a supressão de Bruno lida como do
tenant, o pagamento de Carla casando com o pedido de Ana, o `next_inbound_seq`
de uma conversa guardando outra — não aparece em nenhum cenário isolado e
aparece aqui.

E a assimetria dos três é ela mesma uma afirmação: **a resposta e o bloqueio só
mordem quando o toque tenta sair; o pagamento morde na hora.** É a defesa em
profundidade da D7 — o cancelamento imediato do handler de domínio cobre a ordem
comum, e a revalidação no disparo cobre tudo o que chegou depois.

Nada aqui espera por tempo: cada passo espera por um predicado contra o banco
(decisão 56/61), e a passagem das horas é o `due_at` de um toque indo para o
passado — que é a única coisa que o relógio mudaria.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta

import psycopg
from psycopg.types.json import Jsonb

from agents_runtime.agent_core.responder import FIXED_REPLY
from agents_runtime.app import run
from agents_runtime.config import QueueingConfig
from agents_runtime.dispatch.consent import BLOCK_BUTTON_ID
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

TOUCH_ONE = "Vi que ficou algo no carrinho."


def an_order(external_id: str, *, total: str = "199.90") -> dict:
    return {
        "external_id": external_id,
        "total": total,
        "currency": "BRL",
        "items": [{"sku": "A1", "qty": 1}],
        "customer": {"external_id": f"cust-{external_id}", "name": "Cliente"},
    }


def platform_event(
    conn: psycopg.Connection, store_account: str, event_type: str, payload: dict
) -> None:
    conn.execute(
        "select * from internal.ingest_webhook('shopify', %s, %s, %s, %s)",
        (store_account, unique_id("evt"), event_type, Jsonb(payload)),
    )


def contact_writes(conn: psycopg.Connection, number: str, phone: str, message: dict) -> None:
    """O contato falando — a mesma porta da Cloud, com o debounce encurtado.

    30ms em vez dos 10s canônicos porque o debounce é config e a única coisa que
    este teste não quer medir é a espera; o coalescer é o mesmo, a geração é a
    mesma, e o que sai do outro lado é a resposta ao conjunto completo.
    """
    conn.execute(
        "select * from internal.ingest_webhook('meta', %s, %s, 'message_inbound', %s,"
        " interval '30 milliseconds')",
        (number, unique_id("evt"), Jsonb({"from": phone, "message": message})),
    )


def make_due(conn: psycopg.Connection, phone: str, touch_number: int) -> None:
    """Seis horas depois, para este contato — expresso no único fato que o tempo
    mudaria, e não num `sleep` que nada observa."""
    conn.execute(
        """
        update public.scheduled_touches t
           set due_at = now() - interval '1 second'
          from public.contacts c
         where c.id = t.contact_id and c.phone_e164 = %s and t.touch_number = %s
        """,
        (phone, touch_number),
    )


async def texts_to(admin: psycopg.AsyncConnection, phone: str) -> list[str]:
    cursor = await admin.execute(
        "select payload ->> 'text' from testing.fake_channel_sends"
        " where to_phone_e164 = %s order by id",
        (phone,),
    )
    return [row[0] for row in await cursor.fetchall()]


async def touches_of(admin: psycopg.AsyncConnection, phone: str) -> list[tuple]:
    cursor = await admin.execute(
        """
        select t.touch_number, t.status, t.cancel_reason
          from public.scheduled_touches t
          join public.contacts c on c.id = t.contact_id
         where c.phone_e164 = %s
         order by t.touch_number
        """,
        (phone,),
    )
    return await cursor.fetchall()


async def waiting_for_touch_one(admin: psycopg.AsyncConnection, phone: str):
    async def arrived():
        return await texts_to(admin, phone) == [TOUCH_ONE] or None

    return await eventually(arrived, note=f"the first touch reaching {phone}")


async def waiting_for_cancellation(admin: psycopg.AsyncConnection, phone: str):
    async def cancelled():
        cursor = await admin.execute(
            """
            select t.cancel_reason
              from public.scheduled_touches t
              join public.contacts c on c.id = t.contact_id
             where c.phone_e164 = %s and t.touch_number = 2 and t.status = 'cancelled'
            """,
            (phone,),
        )
        return await cursor.fetchone()

    return await eventually(cancelled, note=f"the second touch of {phone} dying")


async def test_a_real_day_three_contacts_three_endings_and_none_of_them_crossed(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
    tiny_config: QueueingConfig,
) -> None:
    tenant_id = create_tenant(sync_admin)
    store = create_connector_account(sync_admin, tenant_id)
    number = create_channel_account(sync_admin, tenant_id)
    create_funnel(sync_admin, tenant_id, touches=CADENCE)

    ana, bruno, carla = unique_phone(), unique_phone(), unique_phone()
    ana_order, carla_order = unique_id("ord"), unique_id("ord")

    # A manhã: três carrinhos abandonados, quase juntos. Os pedidos de Ana e
    # Carla viajam com o abandono; o de Bruno não existe, e não precisa — um
    # funil sem pedido é o caso de toda plataforma que ainda não manda o pedido
    # junto, e a cadência dele é idêntica.
    platform_event(
        sync_admin,
        store.source_account_id,
        "checkout_abandoned",
        {"phone": ana, "order": an_order(ana_order)},
    )
    platform_event(sync_admin, store.source_account_id, "checkout_abandoned", {"phone": bruno})
    platform_event(
        sync_admin,
        store.source_account_id,
        "checkout_abandoned",
        {"phone": carla, "order": an_order(carla_order, total="349.00")},
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
        # -- os três recebem o primeiro toque, cada um no seu número.
        await waiting_for_touch_one(admin, ana)
        await waiting_for_touch_one(admin, bruno)
        await waiting_for_touch_one(admin, carla)

        # -- Ana responde. O funil acabou de virar conversa.
        contact_writes(
            sync_admin, number.external_account_id, ana, {"text": "dá pra trocar o tamanho?"}
        )

        async def answered():
            return await texts_to(admin, ana) == [TOUCH_ONE, FIXED_REPLY] or None

        await eventually(answered, note="the agent answering Ana like any other message")

        # -- Bruno toca em Bloquear. O id é o que NÓS emitimos no botão, e quem
        # o lê é o motor: nenhum modelo opina sobre uma permissão.
        contact_writes(
            sync_admin,
            number.external_account_id,
            bruno,
            {"type": "interactive", "button_reply": {"id": BLOCK_BUTTON_ID, "title": "Bloquear"}},
        )

        async def suppressed():
            cursor = await admin.execute(
                """
                select s.reason, s.created_by
                  from public.suppression_list s
                  join public.contacts c on c.id = s.contact_id
                 where c.phone_e164 = %s
                """,
                (bruno,),
            )
            return await cursor.fetchone()

        assert await eventually(suppressed, note="Bruno's block landing on the list") == (
            "explicit_block",
            "system",
        )

        # -- Carla paga. Este é o único dos três cujo toque nº 2 morre SEM ter
        # vencido: o handler de domínio cancela na hora (D7), e é essa diferença
        # que separa "o mundo mudou" de "o toque tentou sair e foi barrado".
        platform_event(
            sync_admin,
            store.source_account_id,
            "order_paid",
            {"phone": carla, "order": an_order(carla_order, total="349.00")},
        )
        assert await waiting_for_cancellation(admin, carla) == ("stale_order_paid",)

        # -- seis horas depois, para Ana e Bruno. Aqui os toques VENCEM, e é a
        # escada que os recusa, cada um com a sua palavra.
        make_due(sync_admin, ana, 2)
        make_due(sync_admin, bruno, 2)

        assert await waiting_for_cancellation(admin, ana) == ("stale_newer_message",)
        assert await waiting_for_cancellation(admin, bruno) == ("suppressed_block",)

        # -- e no fim da tarde Ana compra, dentro da janela de atribuição.
        platform_event(
            sync_admin,
            store.source_account_id,
            "order_paid",
            {"phone": ana, "order": an_order(ana_order)},
        )

        async def both_credited():
            cursor = await admin.execute("select count(*) from public.funnel_conversions")
            row = await cursor.fetchone()
            return row if row[0] == 2 else None

        await eventually(both_credited, note="both conversions credited")
    finally:
        stop.set()
        await asyncio.wait_for(running, DEADLINE)

    # --- o dia, lido de trás para frente -------------------------------------

    # Ana ouviu o toque e a resposta ao que ela escreveu. Bruno ouviu o toque e
    # a resposta ao que ELE fez — porque tocar num botão é escrever, e o
    # bloqueio silencia o PROATIVO, não a réplica ao que o contato acabou de
    # mandar (RF-034: resposta reativa nunca é limitada). Carla, só o toque.
    #
    # Esta linha foi escrita errada na primeira versão deste arquivo, e o teste
    # reprovou dizendo exatamente isto. Fica registrada porque a intuição errada
    # é comum: "bloqueado" soa como "mudo", e o produto — corretamente — não
    # deixa de responder a quem falou com ele.
    assert await texts_to(admin, ana) == [TOUCH_ONE, FIXED_REPLY]
    assert await texts_to(admin, bruno) == [TOUCH_ONE, FIXED_REPLY]
    assert await texts_to(admin, carla) == [TOUCH_ONE]

    sent_first = (1, "sent", None)
    assert await touches_of(admin, ana) == [sent_first, (2, "cancelled", "stale_newer_message")]
    assert await touches_of(admin, bruno) == [sent_first, (2, "cancelled", "suppressed_block")]
    assert await touches_of(admin, carla) == [sent_first, (2, "cancelled", "stale_order_paid")]

    # A supressão é de Bruno e de mais ninguém. Um escopo errado aqui é o bug
    # que cala uma loja inteira, e nenhum cenário de um contato só o veria.
    listed = await (
        await admin.execute(
            "select c.phone_e164 from public.suppression_list s"
            " join public.contacts c on c.id = s.contact_id"
        )
    ).fetchall()
    assert listed == [(bruno,)]

    # As duas conversões apontam para o toque que falou com AQUELE contato, com
    # o valor do pedido DAQUELE contato. Trocar os dois seria um relatório de
    # receita que fecha e mente.
    credited = await (
        await admin.execute(
            """
            select c.phone_e164, fc.amount, fc.currency, t.touch_number, o.external_id
              from public.funnel_conversions fc
              join public.contacts c on c.id = fc.contact_id
              join public.scheduled_touches t on t.id = fc.scheduled_touch_id
              join public.orders o on o.id = fc.order_id
             order by fc.amount
            """
        )
    ).fetchall()
    assert [(row[0], str(row[1]), row[2], row[3], row[4]) for row in credited] == [
        (ana, "199.90", "BRL", 1, ana_order),
        (carla, "349.00", "BRL", 1, carla_order),
    ]

    # E Bruno, que bloqueou, não gerou receita nenhuma para ninguém.
    assert bruno not in [row[0] for row in credited]


async def test_the_blocked_contact_never_hears_from_the_next_funnel_either(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
    tiny_config: QueueingConfig,
) -> None:
    """O dia seguinte de Bruno, e o alvo da guarda que o teste acima não tem.

    Lá a supressão barra o toque nº 2 de um funil que já tinha falado com ele.
    Aqui ela barra o PRIMEIRO toque de um funil novo, nascido depois do bloqueio
    — que é o que "removido dos proativos" quer dizer, e o que um lojista assume
    quando lê a palavra. Sem esta prova, o bloqueio poderia ser apenas "este
    funil para", e o próximo abandono de amanhã voltaria a falar com ele.
    """
    tenant_id = create_tenant(sync_admin)
    store = create_connector_account(sync_admin, tenant_id)
    number = create_channel_account(sync_admin, tenant_id)
    create_funnel(sync_admin, tenant_id, touches=CADENCE)
    phone = unique_phone()

    platform_event(sync_admin, store.source_account_id, "checkout_abandoned", {"phone": phone})

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
        await waiting_for_touch_one(admin, phone)

        contact_writes(
            sync_admin,
            number.external_account_id,
            phone,
            {"type": "interactive", "button_reply": {"id": BLOCK_BUTTON_ID}},
        )

        async def suppressed():
            cursor = await admin.execute(
                "select count(*) from public.suppression_list"
            )
            row = await cursor.fetchone()
            return row if row[0] == 1 else None

        await eventually(suppressed, note="the block landing on the list")

        # O dia seguinte: outro carrinho, outro funil desta mesma ocasião — uma
        # cadência inteiramente nova, cujo primeiro toque nasce já vencido.
        platform_event(sync_admin, store.source_account_id, "checkout_abandoned", {"phone": phone})

        async def refused():
            cursor = await admin.execute(
                "select count(*) from public.scheduled_touches"
                " where status = 'cancelled' and cancel_reason = 'suppressed_block'"
            )
            row = await cursor.fetchone()
            # Os dois toques da cadência nova, mais o nº 2 da antiga quando
            # vencer — aqui só a nova interessa, e ela começa a morrer no nº 1.
            return row if row[0] >= 1 else None

        await eventually(refused, note="the new funnel refused at its very first touch")
    finally:
        stop.set()
        await asyncio.wait_for(running, DEADLINE)

    # O canal ouviu duas coisas, e a distinção entre elas é o teste: o toque
    # PROATIVO que veio antes do bloqueio, e a resposta REATIVA ao toque de
    # botão dele. De proativo, nada mais — nem deste funil, nem do seguinte.
    assert await texts_to(admin, phone) == [TOUCH_ONE, FIXED_REPLY]

    proactive = await (
        await admin.execute(
            "select count(*) from internal.message_outbox where kind = 'funnel_touch'"
        )
    ).fetchone()
    assert proactive == (1,)

    cancelled = await (
        await admin.execute(
            "select count(*) from public.scheduled_touches"
            " where status = 'cancelled' and cancel_reason <> 'suppressed_block'"
        )
    ).fetchone()
    assert cancelled == (0,)

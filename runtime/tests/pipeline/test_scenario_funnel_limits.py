"""Cenário 11, a metade que ainda não tinha motor — cadência, teto e intervalo.

O cenário 11 do `core/testes-e-cicd.md` §3.3 faz quatro afirmações:

    Dispatch: `order_paid` cancela os `scheduled_touches` pendentes; supressão
    bloqueia o proativo; evento obsoleto é descartado; o funil completo respeita
    o limite por contato/24h vigente, a cadência configurada e o intervalo entre
    funis.

As três primeiras já tinham prova contra o motor de verdade — `order_paid` em
`test_scenario_order_paid.py`, a supressão e o obsoleto em
`test_scenario_funnel_dispatch.py` e em `test_dispatch_race.py`. **A quarta não
tinha**, e é a única que fala do funil INTEIRO em vez de um toque: um funil
respeita a cadência ao longo do tempo, e é aí — no segundo e no terceiro toque —
que o teto por contato e o intervalo entre funis passam a existir. Um teste de um
toque só nunca chega a tocá-los, e foi assim que eles ficaram cobertos apenas
por `db` (`test_dispatch_touch_cas.py`) e por unidade (`test_protection_ladder.py`),
onde a escada é chamada à mão e a cadência é uma lista literal.

Aqui não. A composição é a real (`app.run`), os papéis são os de produção, o
único dublê é o canal, e as três coisas são medidas onde o lojista as vê:

  * **a cadência configurada** — os `due_at` que nascem são os `delay` do funil,
    e cada toque só sai quando vence, com o texto que era o dele;
  * **o teto por contato/24h VIGENTE** — a palavra "vigente" é o que este teste
    persegue. O teto não é 1 nem 4: é o que `internal.set_proactive_cap` deixou
    no tenant. Um funil de três toques contra um tenant em 2 sai duas vezes e
    morre na terceira com `rate_limit_24h`;
  * **o intervalo entre funis** — 72h desde um toque de OUTRO funil, com o teto
    aberto o bastante para que a recusa não possa ser do teto (uma guarda sem
    alvo mente), e com o caso positivo do outro lado das 72h para provar que a
    recusa era do intervalo e não de "um segundo funil nunca fala".

A condição de parada segue a decisão 61: o último observável — o canal com a
mensagem, ou o toque cancelado —, nunca um estado intermediário. E a passagem do
tempo é DADO, não `sleep`: um toque vence porque seu `due_at` foi movido para o
passado, que é exatamente o que o relógio faria, sem que ninguém espere por ele.
"""

import asyncio
import uuid
from dataclasses import replace
from datetime import timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agents_runtime.app import run
from agents_runtime.config import QueueingConfig
from agents_runtime.dispatch.ladder import FUNNEL_COOLDOWN
from tests.db.factories import (
    create_channel_account,
    create_connector_account,
    create_tenant,
    unique_id,
    unique_phone,
)
from tests.db.factories_e3 import create_funnel, create_scheduled_touch
from tests.pipeline.test_scenarios_a import DEADLINE, eventually
from tests.support.fake_channel import FakeChannel

#: Three touches, three different texts, three different delays. The texts are
#: what make "the cadence was respected" an assertion instead of a count: two
#: sends could be touch nº 1 twice.
THREE_TOUCHES = [
    {"n": 1, "delay": "PT0S", "copy_base": "Vi que ficou algo no carrinho."},
    {"n": 2, "delay": "PT6H", "copy_base": "Ainda dá tempo."},
    {"n": 3, "delay": "PT24H", "copy_base": "Última chamada."},
]

ONE_TOUCH = [{"n": 1, "delay": "PT0S", "copy_base": "Vi que ficou algo no carrinho."}]


def set_cap(conn: psycopg.Connection, tenant_id: uuid.UUID, value: int) -> None:
    """The only write path to `tenants.proactive_max_per_contact_24h` (D1).

    Through the function, never through an UPDATE: the column has a trigger that
    refuses a change that did not come through here, and a test that arranged it
    by hand would be arranging a state production cannot reach.
    """
    conn.execute("select internal.set_proactive_cap(%s, %s, null)", (tenant_id, value))


def abandon(conn: psycopg.Connection, store_account: str, phone: str) -> None:
    conn.execute(
        "select * from internal.ingest_webhook('shopify', %s, %s, 'checkout_abandoned', %s)",
        (store_account, unique_id("evt"), Jsonb({"phone": phone})),
    )


def make_due(conn: psycopg.Connection, funnel_id: uuid.UUID, touch_number: int) -> None:
    """Time passing, expressed as the only fact time changes here.

    Not a `sleep` and not a fake clock: the dispatcher's sweep asks `due_at <=
    now()`, so a touch six hours away becomes a touch that is due the moment its
    `due_at` is in the past. The wait that follows is a predicate against the
    database, which is the harness doctrine of E1 and the decisão 56/61.
    """
    conn.execute(
        "update public.scheduled_touches set due_at = now() - interval '1 second'"
        " where funnel_id = %s and touch_number = %s",
        (funnel_id, touch_number),
    )


@pytest.fixture
def engine_config(tiny_config: QueueingConfig) -> QueueingConfig:
    """The canonical composition with the sweep at test speed — the one rhythm
    the pipeline level is allowed to shorten (the minute tick is config)."""
    return replace(tiny_config, dispatcher_tick=timedelta(milliseconds=50))


async def texts_sent(admin: psycopg.AsyncConnection) -> list[str]:
    cursor = await admin.execute(
        "select payload ->> 'text' from testing.fake_channel_sends order by id"
    )
    return [row[0] for row in await cursor.fetchall()]


async def touch_states(admin: psycopg.AsyncConnection, funnel_id: uuid.UUID) -> list[tuple]:
    cursor = await admin.execute(
        "select touch_number, status, cancel_reason from public.scheduled_touches"
        " where funnel_id = %s order by touch_number",
        (funnel_id,),
    )
    return await cursor.fetchall()


async def test_the_cadence_is_honoured_touch_by_touch_until_the_tenants_ceiling_stops_it(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
    engine_config: QueueingConfig,
) -> None:
    """O funil inteiro, contra o motor: três toques, dois saem, o terceiro morre.

    O teto é 2 porque o admin o colocou em 2. Se fosse o padrão (1) o segundo
    toque já morreria e a cadência nunca seria exercida; se fosse o teto da
    plataforma (4) os três sairiam e o limite não teria alvo. O número do meio é
    o único que prova as duas metades da regra no mesmo fluxo.
    """
    tenant_id = create_tenant(sync_admin)
    set_cap(sync_admin, tenant_id, 2)
    store = create_connector_account(sync_admin, tenant_id)
    create_channel_account(sync_admin, tenant_id)
    funnel = create_funnel(sync_admin, tenant_id, touches=THREE_TOUCHES)
    phone = unique_phone()

    abandon(sync_admin, store.source_account_id, phone)

    stop = asyncio.Event()
    running = asyncio.create_task(
        run(
            dsn,
            stop=stop,
            config=engine_config,
            channel=FakeChannel(dsn),
            worker_set_role="worker_role",
            sender_set_role="sender_role",
        )
    )
    try:

        async def first_arrived():
            return await texts_sent(admin) or None

        assert await eventually(first_arrived, note="the first touch reaching the channel") == [
            "Vi que ficou algo no carrinho."
        ]

        # A cadência configurada É os `due_at`, e eles são medidos do EVENTO,
        # nunca do agendamento (`start_funnel_run`). Seis e vinte e quatro horas
        # é o que o funil disse; qualquer outra coisa é o produto entregando uma
        # cadência que o lojista não configurou.
        cursor = await admin.execute(
            "select touch_number, due_at - event_at from public.scheduled_touches"
            " where funnel_id = %s order by touch_number",
            (funnel.id,),
        )
        assert await cursor.fetchall() == [
            (1, timedelta(0)),
            (2, timedelta(hours=6)),
            (3, timedelta(hours=24)),
        ]

        # E enquanto não vencem, não saem: o segundo e o terceiro seguem
        # `pending` com o primeiro já entregue. Sem esta linha, "a cadência foi
        # respeitada" seria só "nada mais chegou ainda".
        assert await touch_states(admin, funnel.id) == [
            (1, "sent", None),
            (2, "pending", None),
            (3, "pending", None),
        ]

        # Seis horas depois.
        make_due(sync_admin, funnel.id, 2)

        async def second_arrived():
            texts = await texts_sent(admin)
            return texts if len(texts) == 2 else None

        assert await eventually(second_arrived, note="the second touch of the cadence") == [
            "Vi que ficou algo no carrinho.",
            "Ainda dá tempo.",
        ]

        # Mais dezoito. O contato já recebeu dois proativos dentro da janela de
        # 24h, e o teto do tenant é dois.
        make_due(sync_admin, funnel.id, 3)

        async def third_refused():
            cursor = await admin.execute(
                "select cancel_reason from public.scheduled_touches"
                " where funnel_id = %s and touch_number = 3 and status = 'cancelled'",
                (funnel.id,),
            )
            return await cursor.fetchone()

        assert await eventually(third_refused, note="the third touch hitting the ceiling") == (
            "rate_limit_24h",
        )
    finally:
        stop.set()
        await asyncio.wait_for(running, DEADLINE)

    # O canal ouviu duas vezes, e o terceiro toque não deixou rastro de envio
    # nenhum: nem outbox, nem mensagem na conversa.
    assert await texts_sent(admin) == [
        "Vi que ficou algo no carrinho.",
        "Ainda dá tempo.",
    ]
    spine = await (
        await admin.execute(
            "select (select count(*) from internal.message_outbox),"
            "       (select count(*) from public.messages where direction = 'outbound')"
        )
    ).fetchone()
    assert spine == (2, 2)


async def test_a_second_funnel_inside_the_seventy_two_hours_never_speaks(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
    engine_config: QueueingConfig,
) -> None:
    """O intervalo entre funis, com o teto aberto para que ele não possa mentir.

    O contato levou um toque de um funil de PIX há uma hora. O carrinho que ele
    abandona agora é de OUTRO funil, e RF-034 diz que ele tem 72h de sossego
    entre funis diferentes — a cadência é o espaçamento dentro de um funil, o
    intervalo é o espaçamento entre eles.

    O teto do tenant vai a 4 de propósito. Com o padrão 1 o toque morreria com
    `rate_limit_24h` e este teste passaria sem nunca ter chegado ao degrau que
    diz testar: uma guarda sem alvo mente.
    """
    tenant_id = create_tenant(sync_admin)
    set_cap(sync_admin, tenant_id, 4)
    store = create_connector_account(sync_admin, tenant_id)
    channel = create_channel_account(sync_admin, tenant_id)
    phone = unique_phone()

    earlier = _a_touch_that_already_went_out(
        sync_admin, tenant_id, channel.id, phone, sent_ago_seconds=3600
    )
    create_funnel(sync_admin, tenant_id, occasion="checkout_abandoned", touches=ONE_TOUCH)

    abandon(sync_admin, store.source_account_id, phone)

    stop = asyncio.Event()
    running = asyncio.create_task(
        run(
            dsn,
            stop=stop,
            config=engine_config,
            channel=FakeChannel(dsn),
            worker_set_role="worker_role",
            sender_set_role="sender_role",
        )
    )
    try:

        async def refused():
            cursor = await admin.execute(
                "select cancel_reason from public.scheduled_touches"
                " where funnel_id <> %s and status = 'cancelled'",
                (earlier,),
            )
            return await cursor.fetchone()

        assert await eventually(refused, note="the second funnel meeting the cooldown") == (
            "funnel_cooldown_72h",
        )
    finally:
        stop.set()
        await asyncio.wait_for(running, DEADLINE)

    # O canal nunca ouviu falar deste segundo funil: a única linha de outbox é a
    # que o arranjo escreveu pelo funil antigo.
    assert await texts_sent(admin) == []
    outbox = await (
        await admin.execute("select count(*) from internal.message_outbox")
    ).fetchone()
    assert outbox == (1,)


async def test_the_same_second_funnel_speaks_once_the_seventy_two_hours_are_past(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
    engine_config: QueueingConfig,
) -> None:
    """O controle do teste acima, e a razão de ele existir: sem esta prova, a
    recusa anterior poderia ser "um contato que já foi tocado nunca é tocado de
    novo", que é uma regra que este produto não tem.

    A única diferença no arranjo é a idade do toque anterior — do lado de fora
    das 72h em vez de dentro.
    """
    tenant_id = create_tenant(sync_admin)
    set_cap(sync_admin, tenant_id, 4)
    store = create_connector_account(sync_admin, tenant_id)
    channel = create_channel_account(sync_admin, tenant_id)
    phone = unique_phone()

    _a_touch_that_already_went_out(
        sync_admin,
        tenant_id,
        channel.id,
        phone,
        sent_ago_seconds=int(FUNNEL_COOLDOWN.total_seconds()) + 3600,
    )
    create_funnel(sync_admin, tenant_id, occasion="checkout_abandoned", touches=ONE_TOUCH)

    abandon(sync_admin, store.source_account_id, phone)

    stop = asyncio.Event()
    running = asyncio.create_task(
        run(
            dsn,
            stop=stop,
            config=engine_config,
            channel=FakeChannel(dsn),
            worker_set_role="worker_role",
            sender_set_role="sender_role",
        )
    )
    try:

        async def delivered():
            return await texts_sent(admin) or None

        assert await eventually(delivered, note="the second funnel finally allowed") == [
            "Vi que ficou algo no carrinho."
        ]
    finally:
        stop.set()
        await asyncio.wait_for(running, DEADLINE)

    assert await touch_states(admin, _the_new_funnel(sync_admin, tenant_id)) == [(1, "sent", None)]


def _the_new_funnel(conn: psycopg.Connection, tenant_id: uuid.UUID) -> uuid.UUID:
    return conn.execute(
        "select id from public.funnels where tenant_id = %s and occasion = 'checkout_abandoned'",
        (tenant_id,),
    ).fetchone()[0]


def _a_touch_that_already_went_out(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    channel_account_id: uuid.UUID,
    phone: str,
    *,
    sent_ago_seconds: int,
) -> uuid.UUID:
    """Um toque de um funil ANTERIOR, completo: contato, conversa, outbox, `sent`.

    A linha de outbox não é decoração. O degrau das 24h conta `message_outbox` e
    o das 72h conta `scheduled_touches.sent_at`; um toque `sent` sem outbox seria
    um arranjo que a produção não sabe produzir, e faria o teto parecer aberto
    por um motivo errado. Aqui os dois números são o mesmo toque.

    Devolve o `funnel_id` do funil antigo, que é o que o teste usa para dizer
    "o outro".
    """
    contact_id = conn.execute(
        "insert into public.contacts (tenant_id, phone_e164) values (%s, %s) returning id",
        (tenant_id, phone),
    ).fetchone()[0]
    conversation_id = conn.execute(
        "insert into public.conversations (tenant_id, contact_id, channel_account_id,"
        " origin_occasion) values (%s, %s, %s, 'pix_pending') returning id",
        (tenant_id, contact_id, channel_account_id),
    ).fetchone()[0]

    funnel = create_funnel(conn, tenant_id, occasion="pix_pending", touches=ONE_TOUCH)
    outbox_id = conn.execute(
        """
        insert into internal.message_outbox
            (tenant_id, conversation_id, contact_id, channel_account_id,
             kind, payload, idempotency_key, status, created_at)
        values (%s, %s, %s, %s, 'funnel_touch', %s, %s, 'sent',
                now() - make_interval(secs => %s))
        returning id
        """,
        (
            tenant_id,
            conversation_id,
            contact_id,
            channel_account_id,
            Jsonb({"text": "Seu PIX está esperando."}),
            unique_id("touch"),
            sent_ago_seconds,
        ),
    ).fetchone()[0]

    create_scheduled_touch(
        conn,
        tenant_id,
        funnel.id,
        contact_id,
        conversation_id=conversation_id,
        due_in_seconds=-sent_ago_seconds,
        event_age_seconds=sent_ago_seconds + 60,
        status="sent",
        sent_ago_seconds=sent_ago_seconds,
        outbox_id=outbox_id,
    )
    return funnel.id

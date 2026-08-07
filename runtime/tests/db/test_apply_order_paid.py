"""O pagamento cancela o funil, e credita o que recuperou — `internal.apply_order_paid`.

O S3 fez o abandono virar cadência e o S4 deu ao toque vencido uma porta de
saída com escada. Faltava o outro lado do produto: **o contato pagou**. A partir
daqui `order_paid` deixa de ser tipo não suportado e ganha handler próprio, com
três efeitos numa transação só (D4, D7 e D8 do `docs/plano-e3-recuperacao.md`):

  1. **o espelho** (`orders`/`customers`) — a fatia do RF-070 que o marco precisa:
     pagamento e contexto, chaveados por `(connector_account_id, external_id)`;
  2. **o cancelamento imediato** dos toques abertos do contato, com
     `cancel_reason = 'stale_order_paid'` — o vocabulário da escada, nunca um
     sinônimo (D7, e o achado do S2: dois valores para o mesmo fato partem a
     métrica do S11);
  3. **a atribuição de receita** — houve toque ENVIADO dentro da janela
     `tenants.attribution_window_hours` antes do pagamento? Então existe linha em
     `funnel_conversions`. Fato gravado, não consulta: `messages` tem TTL rolante
     de 12 meses e a métrica de receita recuperada precisa sobreviver à purga.

Contrato do payload (decisão desta unidade, no molde do `phone` do E1): o pedido
viaja em `order`, com `external_id` obrigatório; o telefone do contato viaja em
`phone`, E.164 com `+`, e o cliente em `order.customer`. Quem monta esse payload
é a Edge Function do conector (E8) — ou a reconciliação do S8, pela mesma porta.

**Os relógios são todos de dado.** A janela é medida contra o instante do evento
(`webhook_events.received_at`) e o instante do toque (`scheduled_touches.sent_at`),
então "dentro" e "fora" da janela se encenam movendo os fatos no tempo — nenhuma
espera real em lugar nenhum, a mesma técnica de `make_due` e `event_age_seconds`.
"""

import uuid
from datetime import timedelta

import psycopg
import pytest

from agents_runtime.dispatch import ladder
from tests.db.conftest import TwoTenants, as_app_role
from tests.db.factories import (
    ChannelAccount,
    ConnectorAccount,
    create_channel_account,
    create_connector_account,
    create_contact,
    create_webhook_event,
    unique_id,
    unique_phone,
)
from tests.db.factories_e3 import create_funnel, create_order, create_scheduled_touch

CADENCE = [
    {"n": 1, "delay": "PT0S", "copy_base": "Vi que ficou algo no carrinho."},
    {"n": 2, "delay": "PT6H", "copy_base": "Ainda dá tempo."},
]


@pytest.fixture
def store(admin: psycopg.Connection, two_tenants: TwoTenants) -> ConnectorAccount:
    return create_connector_account(admin, two_tenants.a.id)


@pytest.fixture
def number(admin: psycopg.Connection, two_tenants: TwoTenants) -> ChannelAccount:
    return create_channel_account(admin, two_tenants.a.id)


def an_order(
    *,
    external_id: str | None = None,
    total: str | None = "199.90",
    currency: str = "BRL",
    customer_external_id: str | None = None,
    customer_phone: str | None = None,
) -> dict:
    """`total=None` omite a chave — é a plataforma que manda o pagamento sem
    dizer quanto, que existe e é o caso do `amount` nullable."""
    order = {
        "external_id": external_id or unique_id("ord"),
        "currency": currency,
        "status": "unfulfilled",
        "items": [{"sku": "A1", "qty": 2}],
        "tracking_code": "BR123",
    }
    if total is not None:
        order["total"] = total
    if customer_external_id is not None:
        order["customer"] = {
            "external_id": customer_external_id,
            "name": "Ana",
            "email": "ana@example.test",
            "phone": customer_phone,
        }
    return order


def a_payment(
    admin: psycopg.Connection,
    tenant_id: uuid.UUID,
    store: ConnectorAccount,
    *,
    phone: str | None = None,
    order: dict | None = None,
    source_account_id: str | None = None,
    ago: timedelta | None = None,
) -> int:
    """Um `order_paid` já ingerido. `ago` empurra `received_at` para trás — é
    assim que a suíte encena um pagamento drenado tarde sem esperar de verdade."""
    payload: dict = {}
    if phone is not None:
        payload["phone"] = phone
    if order is not None:
        payload["order"] = order

    event_id = create_webhook_event(
        admin,
        tenant_id,
        source="shopify",
        source_account_id=source_account_id or store.source_account_id,
        event_type="order_paid",
        payload=payload,
    )
    if ago is not None:
        admin.execute(
            "update internal.webhook_events set received_at = now() - %s where id = %s",
            (ago, event_id),
        )
    return event_id


def apply_event(conn: psycopg.Connection, event_id: int) -> tuple:
    return conn.execute("select * from internal.apply_domain_event(%s)", (event_id,)).fetchone()


def event_status(conn: psycopg.Connection, event_id: int) -> str:
    return conn.execute(
        "select status from internal.webhook_events where id = %s", (event_id,)
    ).fetchone()[0]


def orders_of(conn: psycopg.Connection, tenant_id: uuid.UUID) -> list[tuple]:
    return conn.execute(
        """
        select external_id, financial_status, total, currency, items, status,
               tracking_code, customer_external_id, contact_id
          from public.orders
         where tenant_id = %s
         order by created_at
        """,
        (tenant_id,),
    ).fetchall()


def touch_states(conn: psycopg.Connection, tenant_id: uuid.UUID) -> list[tuple]:
    return conn.execute(
        """
        select touch_number, status, cancel_reason
          from public.scheduled_touches
         where tenant_id = %s
         order by touch_number, created_at
        """,
        (tenant_id,),
    ).fetchall()


def conversions_of(conn: psycopg.Connection, tenant_id: uuid.UUID) -> list[tuple]:
    return conn.execute(
        """
        select funnel_id, contact_id, scheduled_touch_id, order_id, amount, currency,
               attributed_at
          from public.funnel_conversions
         where tenant_id = %s
         order by attributed_at
        """,
        (tenant_id,),
    ).fetchall()


# --- o espelho (D4) -----------------------------------------------------------


def test_the_payment_mirrors_the_order_and_names_whose_it_is(
    dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants, store: ConnectorAccount
) -> None:
    """Roda como `worker_role`, a identidade de produção: o grant e o SECURITY
    DEFINER fazem parte do que se prova."""
    phone = unique_phone()
    contact_id = admin.execute(
        "insert into public.contacts (tenant_id, phone_e164) values (%s, %s) returning id",
        (two_tenants.a.id, phone),
    ).fetchone()[0]
    order = an_order(customer_external_id=unique_id("cust"), customer_phone=phone)
    event_id = a_payment(admin, two_tenants.a.id, store, phone=phone, order=order)

    with as_app_role(dsn, "worker_role", two_tenants.a.id) as worker:
        status, conversation_id, outbox_id = apply_event(worker, event_id)
        worker.commit()

    assert (status, conversation_id, outbox_id) == ("applied", None, None)
    assert event_status(admin, event_id) == "processed"

    rows = orders_of(admin, two_tenants.a.id)
    assert len(rows) == 1
    (external_id, financial, total, currency, items, fulfilment, tracking, customer, linked) = rows[
        0
    ]
    assert external_id == order["external_id"]
    # `paid` é o que o evento AFIRMA — nunca a palavra crua da plataforma. O
    # CHECK da coluna é o que torna um vocabulário não mapeado ruidoso em vez de
    # silenciosamente diferente de 'paid' (nota de integridade 3).
    assert financial == "paid"
    assert (str(total), currency, fulfilment, tracking) == ("199.90", "BRL", "unfulfilled", "BR123")
    assert items == [{"sku": "A1", "qty": 2}]
    assert customer == order["customer"]["external_id"]
    # O conserto aditivo do S2: quem é o contato deste pedido é fato gravado na
    # hora em que o telefone estava na mão, não um join de três saltos por texto.
    assert linked == contact_id


def test_the_payment_mirrors_the_customer_and_ties_it_to_the_contact(
    admin: psycopg.Connection, two_tenants: TwoTenants, store: ConnectorAccount
) -> None:
    # `contacts.customer_id` existe desde o S2 e até aqui ninguém o escrevia —
    # coluna sem escritor é coluna que mente quando o E5 for lê-la.
    phone = unique_phone()
    contact_id = admin.execute(
        "insert into public.contacts (tenant_id, phone_e164) values (%s, %s) returning id",
        (two_tenants.a.id, phone),
    ).fetchone()[0]
    customer_external_id = unique_id("cust")
    event_id = a_payment(
        admin,
        two_tenants.a.id,
        store,
        phone=phone,
        order=an_order(customer_external_id=customer_external_id, customer_phone=phone),
    )

    apply_event(admin, event_id)

    customer = admin.execute(
        """
        select id, external_id, name, email, phone_e164, connector_account_id
          from public.customers
         where tenant_id = %s
        """,
        (two_tenants.a.id,),
    ).fetchall()
    assert len(customer) == 1
    assert customer[0][1:] == (
        customer_external_id,
        "Ana",
        "ana@example.test",
        phone,
        store.id,
    )
    assert (
        admin.execute(
            "select customer_id from public.contacts where id = %s", (contact_id,)
        ).fetchone()[0]
        == customer[0][0]
    )


def test_a_second_payment_updates_the_mirror_instead_of_duplicating_it(
    admin: psycopg.Connection, two_tenants: TwoTenants, store: ConnectorAccount
) -> None:
    # A chave é `(connector_account_id, external_id)`: plataformas dão ids por
    # loja, e o mesmo pedido chegando duas vezes (webhook + poll do S8) é UM
    # pedido. O espelho é upsert, não append.
    external_id = unique_id("ord")
    order_id = create_order(
        admin, two_tenants.a.id, store.id, external_id=external_id, financial_status="pending"
    )
    event_id = a_payment(
        admin, two_tenants.a.id, store, order=an_order(external_id=external_id, total="250.00")
    )

    apply_event(admin, event_id)

    rows = admin.execute(
        "select id, financial_status, total from public.orders where tenant_id = %s",
        (two_tenants.a.id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == order_id
    assert (rows[0][1], str(rows[0][2])) == ("paid", "250.00")


def test_a_payment_without_an_order_is_a_payload_problem(
    admin: psycopg.Connection, two_tenants: TwoTenants, store: ConnectorAccount
) -> None:
    # O espelho é chaveado pelo pedido; um `order_paid` que não diz QUAL pedido
    # é o adaptador do conector quebrado, e isso um humano conserta. `failed`
    # (não `discarded`) porque reprocessar depois do conserto resolve.
    event_id = a_payment(admin, two_tenants.a.id, store, phone=unique_phone())

    status, _, _ = apply_event(admin, event_id)

    assert status == "invalid_payload"
    assert event_status(admin, event_id) == "failed"
    assert orders_of(admin, two_tenants.a.id) == []


def test_a_payment_from_a_store_we_do_not_know_says_so(
    admin: psycopg.Connection, two_tenants: TwoTenants, store: ConnectorAccount
) -> None:
    # Desfecho próprio, e não `invalid_payload`: o payload estava perfeito, o
    # ARRANJO é que sumiu — a loja foi desconectada entre a ingestão e o
    # processamento. Conserto diferente (reconectar a loja, não corrigir o
    # adaptador), então bucket diferente na métrica do S11. Mesma forma do
    # `no_channel` do E1, inclusive na marca: `failed`, porque um pedido pago
    # que o espelho perde é exatamente o que este marco existe para não perder.
    event_id = a_payment(
        admin,
        two_tenants.a.id,
        store,
        order=an_order(),
        source_account_id=unique_id("store"),
    )

    status, _, _ = apply_event(admin, event_id)

    assert status == "no_store"
    assert event_status(admin, event_id) == "failed"
    assert orders_of(admin, two_tenants.a.id) == []


# --- o cancelamento imediato (D7) ---------------------------------------------


def test_the_cancel_reason_is_the_ladders_own_word(
    admin: psycopg.Connection, two_tenants: TwoTenants, store: ConnectorAccount, number
) -> None:
    # Contrato com o módulo, não com a memória de quem escreveu o SQL: o motivo
    # sai de `dispatch.ladder`, e um sinônimo aqui partiria a métrica "cancelados
    # por motivo" do S11 tão bem quanto achatar nove motivos em quatro.
    assert "stale_order_paid" in ladder.DENIAL_REASONS

    phone = unique_phone()
    contact_id = admin.execute(
        "insert into public.contacts (tenant_id, phone_e164) values (%s, %s) returning id",
        (two_tenants.a.id, phone),
    ).fetchone()[0]
    funnel = create_funnel(admin, two_tenants.a.id, touches=CADENCE)
    create_scheduled_touch(admin, two_tenants.a.id, funnel.id, contact_id, touch_number=1)

    apply_event(admin, a_payment(admin, two_tenants.a.id, store, phone=phone, order=an_order()))

    assert touch_states(admin, two_tenants.a.id) == [(1, "cancelled", "stale_order_paid")]


def test_the_payment_cancels_every_open_touch_and_leaves_the_sent_one_alone(
    admin: psycopg.Connection, two_tenants: TwoTenants, store: ConnectorAccount
) -> None:
    """D7: o pagamento cancela NA HORA, pelo handler de domínio — é dinheiro do
    contato, e a promoção por idade do ADR-4 existe exatamente para ele.

    `sent` não se cancela: um toque que já saiu, apagado, some da janela de 72h
    e da janela de conversão — a mesma razão que `internal.cancel_touch` dá.
    """
    phone = unique_phone()
    contact_id = admin.execute(
        "insert into public.contacts (tenant_id, phone_e164) values (%s, %s) returning id",
        (two_tenants.a.id, phone),
    ).fetchone()[0]
    funnel = create_funnel(admin, two_tenants.a.id, touches=CADENCE)
    create_scheduled_touch(
        admin,
        two_tenants.a.id,
        funnel.id,
        contact_id,
        touch_number=1,
        status="sent",
        sent_ago_seconds=3600,
    )
    create_scheduled_touch(admin, two_tenants.a.id, funnel.id, contact_id, touch_number=2)
    # Já reivindicado pelo dispatcher e esperando na fila: é justamente este que
    # a escada sozinha só pegaria no momento do disparo.
    create_scheduled_touch(
        admin, two_tenants.a.id, funnel.id, contact_id, touch_number=3, status="enqueued"
    )
    # Outro contato do mesmo tenant: quem pagou foi um, não o outro.
    other_contact = create_contact(admin, two_tenants.a.id)
    create_scheduled_touch(admin, two_tenants.a.id, funnel.id, other_contact, touch_number=1)

    apply_event(admin, a_payment(admin, two_tenants.a.id, store, phone=phone, order=an_order()))

    assert touch_states(admin, two_tenants.a.id) == [
        (1, "sent", None),
        (1, "pending", None),
        (2, "cancelled", "stale_order_paid"),
        (3, "cancelled", "stale_order_paid"),
    ]


def test_a_payment_by_somebody_we_never_messaged_cancels_nothing_and_creates_nobody(
    admin: psycopg.Connection, two_tenants: TwoTenants, store: ConnectorAccount
) -> None:
    """O pagamento que chega ANTES de qualquer toque.

    Decisão explícita deste passo: o espelho é escrito, e o contato **não é
    criado**. Um pagamento não é permissão para messaging — criar a pessoa aqui
    plantaria um contato que nenhuma conversa justifica e que a supressão do S6
    teria de proteger. Sem contato não há toque a cancelar nem conversão a
    creditar, e o desfecho continua sendo `applied`: o efeito do evento é o
    espelho.

    O que protege o funil que nasce DEPOIS é o pedido pago já estar no espelho —
    a guarda `order_unpaid` do CAS do S4 passa a ter alvo.
    """
    event_id = a_payment(admin, two_tenants.a.id, store, phone=unique_phone(), order=an_order())

    status, _, _ = apply_event(admin, event_id)

    assert status == "applied"
    assert len(orders_of(admin, two_tenants.a.id)) == 1
    assert orders_of(admin, two_tenants.a.id)[0][8] is None
    assert (
        admin.execute(
            "select count(*) from public.contacts where tenant_id = %s", (two_tenants.a.id,)
        ).fetchone()[0]
        == 0
    )
    assert conversions_of(admin, two_tenants.a.id) == []


def test_the_payment_of_one_tenant_never_cancels_the_touches_of_another(
    admin: psycopg.Connection, two_tenants: TwoTenants, store: ConnectorAccount
) -> None:
    # SECURITY DEFINER atravessa RLS, então a única coisa que separa os dois é o
    # `where tenant_id` da própria função — e é isso que este teste mede. O
    # telefone é o MESMO nos dois tenants, que é o caso em que confundir é fácil.
    phone = unique_phone()
    for tenant in (two_tenants.a, two_tenants.b):
        contact_id = admin.execute(
            "insert into public.contacts (tenant_id, phone_e164) values (%s, %s) returning id",
            (tenant.id, phone),
        ).fetchone()[0]
        funnel = create_funnel(admin, tenant.id, touches=CADENCE)
        create_scheduled_touch(admin, tenant.id, funnel.id, contact_id, touch_number=1)

    apply_event(admin, a_payment(admin, two_tenants.a.id, store, phone=phone, order=an_order()))

    assert touch_states(admin, two_tenants.a.id) == [(1, "cancelled", "stale_order_paid")]
    assert touch_states(admin, two_tenants.b.id) == [(1, "pending", None)]


# --- a atribuição de receita (D8) ---------------------------------------------


def _a_contact_who_was_touched(
    admin: psycopg.Connection,
    tenant_id: uuid.UUID,
    *,
    phone: str,
    sent_ago: timedelta,
    touch_number: int = 1,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    contact_id = admin.execute(
        """
        insert into public.contacts (tenant_id, phone_e164) values (%s, %s)
        on conflict (tenant_id, phone_e164) do update set phone_e164 = excluded.phone_e164
        returning id
        """,
        (tenant_id, phone),
    ).fetchone()[0]
    funnel = create_funnel(admin, tenant_id, touches=CADENCE)
    touch_id = create_scheduled_touch(
        admin,
        tenant_id,
        funnel.id,
        contact_id,
        touch_number=touch_number,
        status="sent",
        sent_ago_seconds=int(sent_ago.total_seconds()),
    )
    return contact_id, funnel.id, touch_id


def test_a_touch_sent_inside_the_window_credits_the_conversion(
    admin: psycopg.Connection, two_tenants: TwoTenants, store: ConnectorAccount
) -> None:
    """D8: receita recuperada é fato gravado, não consulta.

    O valor e a moeda são COPIADOS, não juntados: `messages` tem TTL rolante de
    12 meses e o contato pode ser purgado no E6 — a linha tem de sobreviver às
    duas coisas.
    """
    phone = unique_phone()
    contact_id, funnel_id, touch_id = _a_contact_who_was_touched(
        admin, two_tenants.a.id, phone=phone, sent_ago=timedelta(hours=2)
    )
    order = an_order(total="349.50", currency="BRL")
    event_id = a_payment(admin, two_tenants.a.id, store, phone=phone, order=order)
    paid_at = admin.execute(
        "select received_at from internal.webhook_events where id = %s", (event_id,)
    ).fetchone()[0]

    apply_event(admin, event_id)

    rows = conversions_of(admin, two_tenants.a.id)
    assert len(rows) == 1
    (funnel, contact, touch, order_id, amount, currency, attributed_at) = rows[0]
    assert (funnel, contact, touch) == (funnel_id, contact_id, touch_id)
    assert (str(amount), currency) == ("349.50", "BRL")
    assert (
        order_id
        == admin.execute(
            "select id from public.orders where tenant_id = %s", (two_tenants.a.id,)
        ).fetchone()[0]
    )
    # O instante do PAGAMENTO, não o do processamento: um evento drenado depois
    # de uma queda credita a receita no dia em que o dinheiro entrou.
    assert attributed_at == paid_at


def test_an_order_with_no_total_credits_the_conversion_without_inventing_a_zero(
    admin: psycopg.Connection, two_tenants: TwoTenants, store: ConnectorAccount
) -> None:
    """Conversão sem valor conhecido é NULL, nunca zero.

    Defeito do S2, corrigido com autorização: `amount NOT NULL` obrigava o
    handler a um `coalesce(total, 0)`, e isso transforma "recuperei uma venda de
    valor desconhecido" em "recuperei R$ 0,00" — duas frases diferentes que
    viram a mesma dentro de um `sum()`, para sempre. NULL preserva a distinção e
    faz a soma ignorar o que ela não sabe; o fato da conversão continua gravado,
    que é o que a métrica de "quantas vendas o funil recuperou" precisa.
    """
    phone = unique_phone()
    _a_contact_who_was_touched(admin, two_tenants.a.id, phone=phone, sent_ago=timedelta(hours=2))

    apply_event(
        admin,
        a_payment(admin, two_tenants.a.id, store, phone=phone, order=an_order(total=None)),
    )

    rows = conversions_of(admin, two_tenants.a.id)
    assert len(rows) == 1
    assert rows[0][4] is None
    # E o espelho concorda: o pedido também não inventou um total.
    assert orders_of(admin, two_tenants.a.id)[0][2] is None


def test_a_touch_sent_before_the_window_credits_nothing(
    admin: psycopg.Connection, two_tenants: TwoTenants, store: ConnectorAccount
) -> None:
    # 30h antes, janela padrão de 24h: o contato pagou, mas não foi este toque
    # que o trouxe. Atribuir seria vender ao lojista um número que ele não pode
    # conferir.
    phone = unique_phone()
    _a_contact_who_was_touched(admin, two_tenants.a.id, phone=phone, sent_ago=timedelta(hours=30))

    apply_event(admin, a_payment(admin, two_tenants.a.id, store, phone=phone, order=an_order()))

    assert conversions_of(admin, two_tenants.a.id) == []
    # E o cancelamento não depende da janela: o funil morre de qualquer forma.
    assert touch_states(admin, two_tenants.a.id) == [(1, "sent", None)]


def test_the_window_is_the_tenants_number_and_not_a_literal(
    admin: psycopg.Connection, two_tenants: TwoTenants
) -> None:
    """`tenants.attribution_window_hours` (D8) — dois tenants, o mesmo fato, e a
    diferença é só a configuração. Um literal em SQL passaria nos dois."""
    admin.execute(
        "update public.tenants set attribution_window_hours = 1 where id = %s",
        (two_tenants.a.id,),
    )
    admin.execute(
        "update public.tenants set attribution_window_hours = 48 where id = %s",
        (two_tenants.b.id,),
    )

    for tenant in (two_tenants.a, two_tenants.b):
        store = create_connector_account(admin, tenant.id)
        phone = unique_phone()
        _a_contact_who_was_touched(admin, tenant.id, phone=phone, sent_ago=timedelta(hours=2))
        apply_event(admin, a_payment(admin, tenant.id, store, phone=phone, order=an_order()))

    assert conversions_of(admin, two_tenants.a.id) == []
    assert len(conversions_of(admin, two_tenants.b.id)) == 1


def test_the_last_touch_before_the_payment_is_the_one_credited(
    admin: psycopg.Connection, two_tenants: TwoTenants, store: ConnectorAccount
) -> None:
    # Atribuição de último toque, e a razão é o que a linha significa: "o que
    # falou com esta pessoa por último antes de ela pagar". A UNIQUE em
    # `order_id` já garante que um pagamento credita um funil só.
    phone = unique_phone()
    contact_id, funnel_id, _first = _a_contact_who_was_touched(
        admin, two_tenants.a.id, phone=phone, sent_ago=timedelta(hours=10)
    )
    latest = create_scheduled_touch(
        admin,
        two_tenants.a.id,
        funnel_id,
        contact_id,
        touch_number=2,
        status="sent",
        sent_ago_seconds=1800,
    )

    apply_event(admin, a_payment(admin, two_tenants.a.id, store, phone=phone, order=an_order()))

    rows = conversions_of(admin, two_tenants.a.id)
    assert len(rows) == 1
    assert rows[0][2] == latest


def test_a_touch_sent_after_the_payment_credits_nothing(
    admin: psycopg.Connection, two_tenants: TwoTenants, store: ConnectorAccount
) -> None:
    """O pagamento é de 3h atrás e o toque saiu há 1h — depois do dinheiro.

    Isso acontece de verdade: o job de `order_paid` esperando numa fila drenada
    enquanto o dispatcher segue trabalhando. Creditar aqui inverteria causa e
    efeito e inflaria a única métrica que o lojista comprou.
    """
    phone = unique_phone()
    _a_contact_who_was_touched(admin, two_tenants.a.id, phone=phone, sent_ago=timedelta(hours=1))
    event_id = a_payment(
        admin, two_tenants.a.id, store, phone=phone, order=an_order(), ago=timedelta(hours=3)
    )

    apply_event(admin, event_id)

    assert conversions_of(admin, two_tenants.a.id) == []


def test_a_redelivered_payment_credits_exactly_once(
    admin: psycopg.Connection, two_tenants: TwoTenants, store: ConnectorAccount
) -> None:
    # pgmq reentrega por qualquer motivo normal (VT vencido, crash depois do
    # commit). Duas linhas aqui dobrariam o número pelo qual o lojista julga o
    # produto — e a UNIQUE em `order_id` é a segunda tranca da mesma porta.
    phone = unique_phone()
    _a_contact_who_was_touched(admin, two_tenants.a.id, phone=phone, sent_ago=timedelta(hours=2))
    event_id = a_payment(admin, two_tenants.a.id, store, phone=phone, order=an_order())

    first = apply_event(admin, event_id)
    second = apply_event(admin, event_id)

    assert (first[0], second[0]) == ("applied", "already_applied")
    assert len(conversions_of(admin, two_tenants.a.id)) == 1
    assert len(orders_of(admin, two_tenants.a.id)) == 1


def test_a_second_payment_of_the_same_order_never_credits_a_second_conversion(
    admin: psycopg.Connection, two_tenants: TwoTenants, store: ConnectorAccount
) -> None:
    # Não é reentrega: são dois eventos diferentes sobre o MESMO pedido (o
    # `paid` do webhook e o `paid` do poll de reconciliação do S8, que entram
    # pela mesma porta com ids externos distintos — D5).
    phone = unique_phone()
    _a_contact_who_was_touched(admin, two_tenants.a.id, phone=phone, sent_ago=timedelta(hours=2))
    order = an_order()

    apply_event(admin, a_payment(admin, two_tenants.a.id, store, phone=phone, order=order))
    apply_event(admin, a_payment(admin, two_tenants.a.id, store, phone=phone, order=order))

    assert len(conversions_of(admin, two_tenants.a.id)) == 1


# --- o alvo da guarda do CAS ---------------------------------------------------


def test_an_abandonment_carrying_an_order_links_every_touch_to_it(
    admin: psycopg.Connection, two_tenants: TwoTenants, store: ConnectorAccount, number
) -> None:
    """A guarda `order_unpaid` do CAS do S4 revalida por `scheduled_touches.order_id`.

    Até aqui nenhum toque tinha um: `apply_domain_event` chamava
    `start_funnel_run` sem pedido e a guarda revalidava contra NULL, ou seja,
    contra nada. O espelho do S5 é o que finalmente lhe dá alvo — e o abandono
    que carrega um pedido é quem o planta.
    """
    create_funnel(admin, two_tenants.a.id, touches=CADENCE)
    phone = unique_phone()
    external_id = unique_id("ord")
    event_id = create_webhook_event(
        admin,
        two_tenants.a.id,
        source="shopify",
        source_account_id=store.source_account_id,
        event_type="checkout_abandoned",
        payload={"phone": phone, "order": an_order(external_id=external_id)},
    )

    status, _, _ = apply_event(admin, event_id)

    assert status == "applied"
    mirrored = admin.execute(
        "select id, financial_status, contact_id from public.orders where tenant_id = %s",
        (two_tenants.a.id,),
    ).fetchone()
    # Um abandono não afirma nada sobre pagamento: o espelho nasce `pending`.
    assert mirrored[1] == "pending"
    assert (
        mirrored[2]
        == admin.execute(
            "select id from public.contacts where tenant_id = %s and phone_e164 = %s",
            (two_tenants.a.id, phone),
        ).fetchone()[0]
    )

    linked = admin.execute(
        "select distinct order_id from public.scheduled_touches where tenant_id = %s",
        (two_tenants.a.id,),
    ).fetchall()
    assert linked == [(mirrored[0],)]


def test_an_abandonment_without_an_order_still_becomes_a_cadence(
    admin: psycopg.Connection, two_tenants: TwoTenants, store: ConnectorAccount, number
) -> None:
    # O espelho é aditivo: a plataforma que ainda não manda o pedido no evento
    # de abandono continua produzindo funil, com a guarda apontando para NULL —
    # exatamente o que ela fazia antes deste passo.
    create_funnel(admin, two_tenants.a.id, touches=CADENCE)
    event_id = create_webhook_event(
        admin,
        two_tenants.a.id,
        source="shopify",
        source_account_id=store.source_account_id,
        event_type="checkout_abandoned",
        payload={"phone": unique_phone()},
    )

    status, _, _ = apply_event(admin, event_id)

    assert status == "applied"
    assert orders_of(admin, two_tenants.a.id) == []
    assert [row[0] for row in touch_states(admin, two_tenants.a.id)] == [1, 2]


# --- quem pode chamar ----------------------------------------------------------


def test_the_payment_handler_is_not_executable_by_everyone(dsn: str) -> None:
    # SECURITY DEFINER que escreve atravessando tenants: EXECUTE mínimo
    # (ADR-11). O worker é o único consumidor de `q_domain_events`; ingestão,
    # sender e Data API não têm nada a ver com o espelho de pedidos.
    for role in ("authenticated", "sender_role", "ingestion_role"):
        with psycopg.connect(dsn) as conn:
            conn.execute(f"set role {role}")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("select * from internal.apply_order_paid(1)")

"""O abandono vira cadência — `internal.start_funnel_run()` e o roteamento do E1.

O S3 aposenta o toque de texto fixo do E1 (decisão D6): o evento de abandono
não vira mais UMA linha na outbox, vira **a cadência inteira** do funil em
`scheduled_touches`, numa transação só.

A decisão que este arquivo existe para proteger é a **D11**: nenhum toque —
nem o primeiro — vai direto para a outbox. O RF-033 manda checar supressão
antes de TODO envio proativo e o RF-034 soma TODAS as origens na janela de
24h; um caminho rápido que escapasse das duas seria o bug que só apareceria no
cenário 11. A partir daqui existe uma porta de saída só: o dispatcher do S4.

Desfechos continuam sendo dado, não exceção. `no_funnel` entra no vocabulário
de `apply_domain_event`, e os quatro invariantes do handler do E1
(`already_applied`, `invalid_payload`, `no_channel`, evento inexistente
levanta) continuam valendo — os três primeiros em
`tests/db/test_apply_domain_event.py`, e o `already_applied` aqui, porque
provar que a reentrega não cria uma SEGUNDA cadência exige um funil.
"""

import uuid
from datetime import datetime, timedelta

import psycopg
import pytest

from tests.db.conftest import TwoTenants, as_app_role
from tests.db.factories import (
    ChannelAccount,
    create_channel_account,
    create_webhook_event,
    unique_id,
    unique_phone,
)
from tests.db.factories_e3 import create_funnel

# A cadência das provas: o primeiro toque na hora do evento, e mais dois. Os
# `delay` são ISO-8601 porque é o que a coluna `funnels.touches` carrega e o
# que o Postgres sabe ler sozinho — nenhuma tradução no meio.
CADENCE = [
    {"n": 1, "delay": "PT0S", "copy_base": "Vi que ficou algo no carrinho."},
    {"n": 2, "delay": "PT6H", "copy_base": "Ainda dá tempo."},
    {"n": 3, "delay": "PT24H", "copy_base": "Última chamada."},
]
CADENCE_DELAYS = [timedelta(0), timedelta(hours=6), timedelta(hours=24)]


@pytest.fixture
def number(admin: psycopg.Connection, two_tenants: TwoTenants) -> ChannelAccount:
    return create_channel_account(admin, two_tenants.a.id)


def abandonment(
    admin: psycopg.Connection,
    tenant_id: uuid.UUID,
    *,
    phone: str | None = None,
    event_type: str = "checkout_abandoned",
    ago: timedelta | None = None,
) -> tuple[int, str]:
    """Um evento de plataforma já ingerido, e o telefone que viaja nele.

    `ago` empurra `received_at` para trás — é assim que a suíte encena um
    evento atrasado (drenagem pós-queda) sem esperar de verdade.
    """
    phone = phone or unique_phone()
    event_id = create_webhook_event(
        admin, tenant_id, event_type=event_type, payload={"phone": phone}
    )
    if ago is not None:
        admin.execute(
            "update internal.webhook_events set received_at = now() - %s where id = %s",
            (ago, event_id),
        )
    return event_id, phone


def apply_event(conn: psycopg.Connection, event_id: int) -> tuple:
    return conn.execute("select * from internal.apply_domain_event(%s)", (event_id,)).fetchone()


def event_received_at(conn: psycopg.Connection, event_id: int) -> datetime:
    return conn.execute(
        "select received_at from internal.webhook_events where id = %s", (event_id,)
    ).fetchone()[0]


def touches_of(conn: psycopg.Connection, tenant_id: uuid.UUID) -> list[tuple]:
    return conn.execute(
        """
        select touch_number, event_at, due_at, status, cancel_reason, sent_at, outbox_id,
               funnel_id, contact_id, conversation_id
          from public.scheduled_touches
         where tenant_id = %s
         order by touch_number
        """,
        (tenant_id,),
    ).fetchall()


#: Nomes de tabela nunca vêm de dado — só desta lista, como o `APP_ROLES` do
#: conftest faz com `SET ROLE`.
_COUNTABLE = {
    "outbox": "internal.message_outbox",
    "messages": "public.messages",
    "contacts": "public.contacts",
    "conversations": "public.conversations",
}


def count_of(conn: psycopg.Connection, what: str, tenant_id: uuid.UUID) -> int:
    return conn.execute(
        f"select count(*) from {_COUNTABLE[what]} where tenant_id = %s",
        (tenant_id,),
    ).fetchone()[0]


def nothing_was_sent(conn: psycopg.Connection, tenant_id: uuid.UUID) -> None:
    """A asserção central da D11, num lugar só: o funil não fala com ninguém.

    Nem outbox, nem mensagem — as duas coisas que o toque fixo do E1 produzia
    e que agora só o dispatcher do S4 pode produzir.
    """
    assert count_of(conn, "outbox", tenant_id) == 0
    assert count_of(conn, "messages", tenant_id) == 0


# --- a cadência ---------------------------------------------------------------


def test_an_abandonment_materialises_the_whole_cadence(
    dsn: str,
    admin: psycopg.Connection,
    two_tenants: TwoTenants,
    number: ChannelAccount,
) -> None:
    """Roda como `worker_role`, a identidade de produção: o grant e o
    SECURITY DEFINER fazem parte do que se prova."""
    funnel = create_funnel(admin, two_tenants.a.id, touches=CADENCE)
    event_id, phone = abandonment(admin, two_tenants.a.id)
    received_at = event_received_at(admin, event_id)

    with as_app_role(dsn, "worker_role", two_tenants.a.id) as worker:
        status, conversation_id, outbox_id = apply_event(worker, event_id)
        worker.commit()

    assert status == "applied"
    assert conversation_id is not None
    # D11: o desfecho não carrega outbox nenhuma, porque não existe nenhuma.
    assert outbox_id is None

    contact = admin.execute(
        "select id, phone_e164 from public.contacts where tenant_id = %s",
        (two_tenants.a.id,),
    ).fetchone()
    assert contact[1] == phone

    rows = touches_of(admin, two_tenants.a.id)
    assert [row[0] for row in rows] == [1, 2, 3]
    for row, delay in zip(rows, CADENCE_DELAYS, strict=True):
        _n, event_at, due_at, status_, reason, sent_at, outbox, fid, cid, conv = row
        assert event_at == received_at
        assert due_at == received_at + delay
        assert (status_, reason, sent_at, outbox) == ("pending", None, None, None)
        assert (fid, cid, conv) == (funnel.id, contact[0], conversation_id)

    # O nº 1 já nasce vencido — o dispatcher do S4 o pega no tique seguinte.
    assert rows[0][2] <= datetime.now(rows[0][2].tzinfo)

    nothing_was_sent(admin, two_tenants.a.id)

    assert (
        admin.execute(
            "select status from internal.webhook_events where id = %s", (event_id,)
        ).fetchone()[0]
        == "processed"
    )


def test_the_cadence_lands_in_the_open_conversation(
    admin: psycopg.Connection, two_tenants: TwoTenants, number: ChannelAccount
) -> None:
    # O contato já conversa com a loja. A cadência entra NA conversa aberta —
    # uma segunda dividiria o histórico e os contadores de seq, e a guard
    # `next_inbound_seq` do S4 passaria a olhar para a conversa errada.
    create_funnel(admin, two_tenants.a.id, touches=CADENCE)
    phone = unique_phone()
    inbound = admin.execute(
        "select * from internal.ingest_webhook('meta', %s, %s, 'message_inbound', %s)",
        (
            number.external_account_id,
            unique_id("evt"),
            psycopg.types.json.Jsonb({"from": phone, "message": {"text": "oi"}}),
        ),
    ).fetchone()
    existing_conversation = inbound[3]

    event_id, _ = abandonment(admin, two_tenants.a.id, phone=phone)
    status, conversation_id, _ = apply_event(admin, event_id)

    assert status == "applied"
    assert conversation_id == existing_conversation
    assert (
        admin.execute(
            "select count(*) from public.conversations where tenant_id = %s",
            (two_tenants.a.id,),
        ).fetchone()[0]
        == 1
    )
    assert {row[9] for row in touches_of(admin, two_tenants.a.id)} == {existing_conversation}


def test_the_cadence_stops_at_the_funnels_max_touches(
    admin: psycopg.Connection, two_tenants: TwoTenants, number: ChannelAccount
) -> None:
    # `max_touches` é teto DO FUNIL, não teto por contato: ele corta a cadência
    # configurada. Os limites por contato são da escada (RF-034) e nada aqui
    # os antecipa.
    create_funnel(admin, two_tenants.a.id, touches=CADENCE, max_touches=2)
    event_id, _ = abandonment(admin, two_tenants.a.id)

    apply_event(admin, event_id)

    assert [row[0] for row in touches_of(admin, two_tenants.a.id)] == [1, 2]


# --- o instante do evento, e o evento atrasado --------------------------------


def test_event_at_is_the_event_instant_never_the_scheduling_instant(
    admin: psycopg.Connection, two_tenants: TwoTenants, number: ChannelAccount
) -> None:
    # A coluna nasceu sem default exatamente para impedir esta substituição
    # silenciosa (S2). Se o agendamento entrasse no lugar do evento, a escada
    # compararia "mensagem mais nova que o agendamento" — e um contato que
    # respondeu DURANTE a fila apareceria como silencioso.
    create_funnel(admin, two_tenants.a.id, touches=CADENCE)
    event_id, _ = abandonment(admin, two_tenants.a.id, ago=timedelta(minutes=42))
    received_at = event_received_at(admin, event_id)

    apply_event(admin, event_id)

    rows = touches_of(admin, two_tenants.a.id)
    assert {row[1] for row in rows} == {received_at}
    # E a distância até agora sobreviveu: o evento é velho, não recém-nascido.
    assert datetime.now(received_at.tzinfo) - rows[0][1] >= timedelta(minutes=42)


def test_a_late_event_materialises_a_cadence_already_overdue(
    admin: psycopg.Connection, two_tenants: TwoTenants, number: ChannelAccount
) -> None:
    """Drenagem pós-queda: o evento chega 20h atrasado.

    Decisão deste passo: a cadência é ancorada no **evento**, sem piso em
    `now()`. Um evento de 20h atrás com cadência 0h/6h/24h nasce com os toques
    1 e 2 vencidos e o 3 no futuro — e é a escada, no S4, que decide se um
    toque atrasado ainda sai (a janela de 24h do RF-034 é justamente o que
    impede a rajada). Deslocar a cadência para caber no relógio de agora seria
    uma decisão de agendamento tomada no lugar errado, e a única que pode
    ACRESCENTAR envios em vez de suprimi-los.
    """
    create_funnel(admin, two_tenants.a.id, touches=CADENCE)
    event_id, _ = abandonment(admin, two_tenants.a.id, ago=timedelta(hours=20))
    received_at = event_received_at(admin, event_id)

    apply_event(admin, event_id)

    rows = touches_of(admin, two_tenants.a.id)
    now = datetime.now(received_at.tzinfo)
    assert [row[2] for row in rows] == [received_at + delay for delay in CADENCE_DELAYS]
    assert [row[2] < now for row in rows] == [True, True, False]

    # Vencidos, mas ainda assim `pending`: nenhum atalho, nem para o atrasado.
    assert {row[3] for row in rows} == {"pending"}
    nothing_was_sent(admin, two_tenants.a.id)


# --- sem funil ----------------------------------------------------------------


def test_a_disabled_funnel_produces_no_funnel_and_writes_nothing(
    admin: psycopg.Connection, two_tenants: TwoTenants, number: ChannelAccount
) -> None:
    create_funnel(admin, two_tenants.a.id, touches=CADENCE, enabled=False)
    event_id, _ = abandonment(admin, two_tenants.a.id)

    status, conversation_id, outbox_id = apply_event(admin, event_id)

    assert (status, conversation_id, outbox_id) == ("no_funnel", None, None)
    # "Sem nada gravado" é literal: nem toque, nem contato, nem conversa. Um
    # evento que não vira funil não deixa uma pessoa e uma conversa vazia para
    # trás só porque o handler passou por ali.
    assert touches_of(admin, two_tenants.a.id) == []
    assert count_of(admin, "contacts", two_tenants.a.id) == 0
    assert count_of(admin, "conversations", two_tenants.a.id) == 0
    nothing_was_sent(admin, two_tenants.a.id)

    # Rastro, sem falha: reprocessar não mudaria a configuração do lojista.
    assert (
        admin.execute(
            "select status from internal.webhook_events where id = %s", (event_id,)
        ).fetchone()[0]
        == "discarded"
    )


def test_an_occasion_with_no_funnel_at_all_is_the_same_desfecho(
    admin: psycopg.Connection, two_tenants: TwoTenants, number: ChannelAccount
) -> None:
    # Funil do carrinho ligado, evento de PIX: ausência e desligado são o mesmo
    # fato para quem pergunta "esta ocasião tem funil?".
    create_funnel(admin, two_tenants.a.id, occasion="cart_abandoned", touches=CADENCE)
    event_id, _ = abandonment(admin, two_tenants.a.id, event_type="pix_pending")

    status, _, _ = apply_event(admin, event_id)

    assert status == "no_funnel"
    assert touches_of(admin, two_tenants.a.id) == []


def test_a_funnel_of_another_tenant_is_not_a_funnel(
    admin: psycopg.Connection, two_tenants: TwoTenants, number: ChannelAccount
) -> None:
    # O funil está no tenant B; o evento é do A. SECURITY DEFINER atravessa
    # RLS, então a única coisa que impede a troca é o `where tenant_id` da
    # própria função — e é isso que este teste mede.
    create_funnel(admin, two_tenants.b.id, touches=CADENCE)
    event_id, _ = abandonment(admin, two_tenants.a.id)

    status, _, _ = apply_event(admin, event_id)

    assert status == "no_funnel"
    assert touches_of(admin, two_tenants.b.id) == []


# --- reentrega ----------------------------------------------------------------


def test_reapplying_a_processed_event_creates_no_second_cadence(
    admin: psycopg.Connection, two_tenants: TwoTenants, number: ChannelAccount
) -> None:
    # O invariante `already_applied` do E1, agora com o que a reentrega poderia
    # duplicar de verdade: uma cadência inteira. pgmq reentrega por qualquer
    # motivo normal (VT vencido, crash depois do commit) — um carrinho
    # abandonado é um funil, não dois.
    create_funnel(admin, two_tenants.a.id, touches=CADENCE)
    event_id, _ = abandonment(admin, two_tenants.a.id)

    first = apply_event(admin, event_id)
    second = apply_event(admin, event_id)

    assert first[0] == "applied"
    assert second[0] == "already_applied"
    assert [row[0] for row in touches_of(admin, two_tenants.a.id)] == [1, 2, 3]
    nothing_was_sent(admin, two_tenants.a.id)


def test_the_schema_refuses_a_second_cadence_for_the_same_event(
    admin: psycopg.Connection, two_tenants: TwoTenants, number: ChannelAccount
) -> None:
    # A segunda tranca da mesma porta, no molde da UNIQUE da outbox no E1: se
    # a guarda de reentrega fosse burlada, o índice ainda recusaria. Mesmo
    # funil, mesmo contato, mesmo toque, mesmo instante de evento = o mesmo
    # abandono.
    funnel = create_funnel(admin, two_tenants.a.id, touches=CADENCE)
    event_id, _ = abandonment(admin, two_tenants.a.id)
    apply_event(admin, event_id)

    existing = admin.execute(
        """
        select contact_id, conversation_id, event_at, due_at
          from public.scheduled_touches
         where tenant_id = %s and touch_number = 1
        """,
        (two_tenants.a.id,),
    ).fetchone()

    with pytest.raises(psycopg.errors.UniqueViolation):
        admin.execute(
            """
            insert into public.scheduled_touches
                (tenant_id, funnel_id, contact_id, conversation_id,
                 touch_number, event_at, due_at)
            values (%s, %s, %s, %s, 1, %s, %s)
            """,
            (two_tenants.a.id, funnel.id, existing[0], existing[1], existing[2], existing[3]),
        )


# --- quem pode chamar ----------------------------------------------------------


def test_neither_function_is_executable_by_everyone(dsn: str) -> None:
    # SECURITY DEFINER que escreve atravessando tenants: EXECUTE mínimo
    # (ADR-11). O worker é o único consumidor de `q_domain_events`; ingestão,
    # sender e Data API não têm nada a ver com funil.
    calls = (
        "select * from internal.apply_domain_event(1)",
        "select * from internal.start_funnel_run("
        "  '00000000-0000-0000-0000-000000000000'::uuid, 'cart_abandoned',"
        "  '+5511999999999', '00000000-0000-0000-0000-000000000000'::uuid, now())",
    )
    for role in ("authenticated", "sender_role", "ingestion_role"):
        for call in calls:
            with psycopg.connect(dsn) as conn:
                conn.execute(f"set role {role}")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(call)

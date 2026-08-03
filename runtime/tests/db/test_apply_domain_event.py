"""O handler de `q_domain_events`, lado SQL — `internal.apply_domain_event()`.

O toque do fio (prova 1 do marco): um evento de plataforma já ingerido vira UM
registro na outbox, com texto fixo, numa transação só. Não é o funil do E3 —
sem supressão, sem quota, sem rate limit, sem agendamento. É a espinha: o
caminho evento → contato → conversa → outbox existindo de ponta a ponta.

Contrato do payload de plataforma (decisão desta unidade): o telefone do
contato viaja em `phone`, E.164 com `+`. Quem monta esse payload é a Edge
Function do conector (E8) — ou a demo, chamando `ingest_webhook` diretamente.

Desfechos são dados, não exceções: `applied`, `already_applied`, `discarded`,
`invalid_payload`, `no_channel`. Exceção fica reservada para o que É bug — um
job apontando para evento que não existe.
"""

import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.db.conftest import TwoTenants, as_app_role
from tests.db.factories import (
    ChannelAccount,
    create_channel_account,
    create_webhook_event,
    unique_id,
    unique_phone,
)

TOUCH = "Vimos que ficou algo no seu carrinho! Posso ajudar a finalizar? 🧡"


def apply_event(conn: psycopg.Connection, event_id: int, text: str = TOUCH) -> tuple:
    """Chama a função e devolve o outcome como tupla (status, conversa, outbox)."""
    return conn.execute(
        "select * from internal.apply_domain_event(%s, %s)", (event_id, text)
    ).fetchone()


def outbox_rows(conn: psycopg.Connection, tenant_id) -> list[tuple]:
    return conn.execute(
        """
        select kind, payload, idempotency_key, status
          from internal.message_outbox
         where tenant_id = %s
        """,
        (tenant_id,),
    ).fetchall()


@pytest.fixture
def number(admin: psycopg.Connection, two_tenants: TwoTenants) -> ChannelAccount:
    return create_channel_account(admin, two_tenants.a.id)


def abandonment(
    admin: psycopg.Connection,
    tenant_id,
    *,
    phone: str | None = None,
    event_type: str = "checkout_abandoned",
    payload: dict | None = None,
) -> int:
    return create_webhook_event(
        admin,
        tenant_id,
        event_type=event_type,
        payload=payload if payload is not None else {"phone": phone or unique_phone()},
    )


# --- o caminho feliz, com a identidade real ----------------------------------


def test_an_abandonment_becomes_exactly_one_touch(
    dsn: str,
    admin: psycopg.Connection,
    two_tenants: TwoTenants,
    number: ChannelAccount,
) -> None:
    """O teste roda como `worker_role` — a identidade que o handler usa em produção.

    O grant e o SECURITY DEFINER são parte do que se prova: se qualquer um dos
    dois faltar, não é o assert que falha, é a chamada.
    """
    phone = unique_phone()
    event_id = abandonment(admin, two_tenants.a.id, phone=phone)

    with as_app_role(dsn, "worker_role", two_tenants.a.id) as worker:
        status, conversation_id, outbox_id = apply_event(worker, event_id)
        worker.commit()

    assert status == "applied"
    assert conversation_id is not None
    assert outbox_id is not None

    # O contato nasceu do payload, telefone E.164 intacto.
    contact = admin.execute(
        "select phone_e164 from public.contacts where tenant_id = %s",
        (two_tenants.a.id,),
    ).fetchall()
    assert contact == [(phone,)]

    # A conversa carrega a ocasião do evento — é ela que o E3 vai ler para
    # saber de onde a conversa veio.
    origin = admin.execute(
        "select origin_occasion, channel_account_id from public.conversations where id = %s",
        (conversation_id,),
    ).fetchone()
    assert origin == ("checkout_abandoned", number.id)

    # UM registro na outbox: um toque, texto fixo, chave derivada do evento —
    # a reentrega do job jamais poderia cunhar uma segunda chave.
    rows = outbox_rows(admin, two_tenants.a.id)
    assert rows == [("funnel_touch", {"text": TOUCH}, f"touch-{event_id}", "pending")]

    # A mensagem outbound existe, sequenciada pelo contador oficial.
    outbound = admin.execute(
        "select direction, seq, author_type from public.messages where conversation_id = %s",
        (conversation_id,),
    ).fetchall()
    assert outbound == [("outbound", 1, "agent")]

    # E o evento tem desfecho com carimbo — o rastro que a reentrega vai ler.
    event = admin.execute(
        "select status, processed_at is not null from internal.webhook_events where id = %s",
        (event_id,),
    ).fetchone()
    assert event == ("processed", True)


# --- idempotência -------------------------------------------------------------


def test_reapplying_a_processed_event_is_a_no_op(
    admin: psycopg.Connection, two_tenants: TwoTenants, number: ChannelAccount
) -> None:
    # pgmq reentrega: VT vencido, crash depois do commit, qualquer motivo
    # normal. A segunda aplicação precisa ser um não-acontecimento — o cliente
    # que abandonou UM carrinho recebe UM toque.
    event_id = abandonment(admin, two_tenants.a.id)

    first = apply_event(admin, event_id)
    second = apply_event(admin, event_id)

    assert first[0] == "applied"
    assert second[0] == "already_applied"
    assert len(outbox_rows(admin, two_tenants.a.id)) == 1


# --- a conversa ---------------------------------------------------------------


def test_the_touch_reuses_an_open_conversation(
    admin: psycopg.Connection, two_tenants: TwoTenants, number: ChannelAccount
) -> None:
    # O contato já conversa com a loja. O toque entra NA conversa aberta — uma
    # segunda conversa dividiria o histórico e o contador de seq.
    phone = unique_phone()
    inbound = admin.execute(
        "select * from internal.ingest_webhook('meta', %s, %s, 'message_inbound', %s)",
        (
            number.external_account_id,
            unique_id("evt"),
            Jsonb({"from": phone, "message": {"text": "oi"}}),
        ),
    ).fetchone()
    existing_conversation = inbound[3]

    event_id = abandonment(admin, two_tenants.a.id, phone=phone)
    status, conversation_id, _ = apply_event(admin, event_id)

    assert status == "applied"
    assert conversation_id == existing_conversation

    conversations = admin.execute(
        "select count(*) from public.conversations where tenant_id = %s",
        (two_tenants.a.id,),
    ).fetchone()[0]
    assert conversations == 1


# --- desfechos que não tocam --------------------------------------------------


def test_an_unsupported_event_type_is_discarded(
    admin: psycopg.Connection, two_tenants: TwoTenants, number: ChannelAccount
) -> None:
    # `order_paid` cancela funil no E3 — hoje ele é deliberadamente descartado,
    # com rastro. Descartar ≠ falhar: reprocessar não mudaria o resultado.
    event_id = abandonment(admin, two_tenants.a.id, event_type="order_paid")

    status, conversation_id, outbox_id = apply_event(admin, event_id)

    assert (status, conversation_id, outbox_id) == ("discarded", None, None)
    assert outbox_rows(admin, two_tenants.a.id) == []
    assert (
        admin.execute(
            "select status from internal.webhook_events where id = %s", (event_id,)
        ).fetchone()[0]
        == "discarded"
    )


def test_a_payload_without_a_phone_fails_visibly_and_writes_nothing(
    admin: psycopg.Connection, two_tenants: TwoTenants, number: ChannelAccount
) -> None:
    # Dado ruim é problema de dados, não de sorte: reentregar não conserta.
    # O evento marca `failed` (o rastro humano) em vez de rodar a escada de
    # retry até uma DLQ que nunca mudaria de ideia.
    event_id = abandonment(admin, two_tenants.a.id, payload={"total": 199})

    status, _, _ = apply_event(admin, event_id)

    assert status == "invalid_payload"
    assert outbox_rows(admin, two_tenants.a.id) == []
    assert (
        admin.execute(
            "select count(*) from public.contacts where tenant_id = %s",
            (two_tenants.a.id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        admin.execute(
            "select status from internal.webhook_events where id = %s", (event_id,)
        ).fetchone()[0]
        == "failed"
    )


def test_a_tenant_without_an_active_channel_is_not_touched(
    admin: psycopg.Connection, two_tenants: TwoTenants
) -> None:
    # Sem número ativo não há por onde sair. `failed` deixa o rastro; quando o
    # canal conectar, o reprocesso da DLQ (ou um evento novo) resolve.
    event_id = abandonment(admin, two_tenants.b.id)

    status, _, _ = apply_event(admin, event_id)

    assert status == "no_channel"
    assert (
        admin.execute(
            "select status from internal.webhook_events where id = %s", (event_id,)
        ).fetchone()[0]
        == "failed"
    )


def test_a_job_pointing_at_no_event_raises(admin: psycopg.Connection) -> None:
    # Isso não é desfecho, é bug — alguém enfileirou um id que nunca existiu.
    # A exceção sobe, a escada de retry corre e o job morre na DLQ, visível.
    with pytest.raises(psycopg.errors.RaiseException):
        apply_event(admin, 2_000_000_000)
    admin.rollback()


# --- quem pode chamar ----------------------------------------------------------


def test_the_touch_is_not_executable_by_everyone(dsn: str) -> None:
    # SECURITY DEFINER que escreve atravessando tenants: EXECUTE mínimo. O
    # worker toca (teste do caminho feliz); ingestão, sender e Data API não.
    for role in ("authenticated", "sender_role", "ingestion_role"):
        with psycopg.connect(dsn) as conn:
            conn.execute(f"set role {role}")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("select * from internal.apply_domain_event(1, 'x')")

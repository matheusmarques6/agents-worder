"""O handler de `q_domain_events`, lado SQL — os desfechos que NÃO tocam.

Este arquivo nasceu no E1 provando o toque de texto fixo: um evento já ingerido
virava UM registro na outbox, numa transação só. O E3 S3 aposentou esse toque
(decisão D6 do `docs/plano-e3-recuperacao.md`) — a própria migration que o
criou já o declarava andaime ("This is deliberately NOT the E3 funnel"). O
abandono agora vira a **cadência** de um funil, e as três provas que afirmavam
o texto fixo (`applied` com o registro na outbox, a reentrega contada em linhas
de outbox, e o reúso da conversa aberta) morreram junto com a função que o
produzia. As duas últimas renasceram em `tests/db/test_start_funnel_run.py`,
onde existe um funil para elas afirmarem algo.

O que sobrevive aqui é exatamente o que não depende de funil nenhum, e é o que
autorizou a emenda da Lei 1: `already_applied` (provado com cadência no arquivo
novo), `invalid_payload`, `no_channel`, o descarte do tipo não suportado, o
EXECUTE mínimo, e o único caso que **levanta** — um job apontando para evento
que não existe.

Contrato do payload de plataforma (decisão desta unidade): o telefone do
contato viaja em `phone`, E.164 com `+`. Quem monta esse payload é a Edge
Function do conector (E8) — ou a demo, chamando `ingest_webhook` diretamente.

Desfechos são dados, não exceções: `applied`, `already_applied`, `discarded`,
`invalid_payload`, `no_channel`, `no_funnel`. Exceção fica reservada para o que
É bug.

**S10 — a contração.** As chamadas deste arquivo passavam pelo shim N-1 de duas
casas; ele foi removido e elas passaram a ser as de uma casa, que é a que o
`app.py` faz. Ver `apply_event` abaixo: os invariantes são os mesmos, o caminho
até eles é que ficou único.
"""

import psycopg
import pytest

from tests.db.conftest import TwoTenants
from tests.db.factories import (
    ChannelAccount,
    create_channel_account,
    create_webhook_event,
    unique_phone,
)


def apply_event(conn: psycopg.Connection, event_id: int) -> tuple:
    """Chama a função e devolve o outcome como tupla (status, conversa, outbox).

    **S10 — a contração.** Até aqui esta função chamava a forma de DUAS casas,
    o shim N-1 que o S3 deixou para a imagem anterior do runtime sobreviver ao
    deploy, passando o texto aposentado que ele ignorava. O shim foi removido
    (`20260806000012_contract_domain_event_shim.sql`), então a chamada é a de
    uma casa — a mesma que o `app.py` faz.

    **Nenhum invariante deste arquivo mudou.** Os quatro desfechos que ele prova
    — tipo não suportado descartado com rastro, payload sem telefone falhando
    visivelmente e sem escrever nada, tenant sem número ativo em `no_channel`, e
    um id de evento inexistente LEVANTANDO em vez de virar desfecho — são
    desfechos da função de uma casa e sempre foram: o shim nunca fez mais do que
    repassar. O que morreu foi um caminho até ela, não uma afirmação sobre ela.
    """
    return conn.execute(
        "select * from internal.apply_domain_event(%s)", (event_id,)
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


# --- o que morreu com o texto fixo (D6) ---------------------------------------
#
# Três provas viviam aqui e não sobrevivem à aposentadoria do toque fixo, porque
# as três afirmavam o registro que ele produzia na outbox:
#
#   * `test_an_abandonment_becomes_exactly_one_touch` — o caminho feliz do E1,
#     `("funnel_touch", {"text": TOUCH}, "touch-<id>", "pending")`. Sob a D11
#     nenhum toque vai direto para a outbox, então não há registro a afirmar.
#     Sucessor: `test_an_abandonment_materialises_the_whole_cadence`.
#   * `test_reapplying_a_processed_event_is_a_no_op` — contava linhas de outbox
#     para provar a reentrega. O invariante `already_applied` sobreviveu
#     inteiro e agora afirma o que a reentrega poderia duplicar de verdade, uma
#     cadência: `test_reapplying_a_processed_event_creates_no_second_cadence`.
#   * `test_the_touch_reuses_an_open_conversation` — o reúso da conversa
#     aberta, que continua sendo regra e agora exige um funil para chegar a
#     `applied`: `test_the_cadence_lands_in_the_open_conversation`.
#
# Todas as três estão em `tests/db/test_start_funnel_run.py`. O que segue abaixo
# é o que nunca dependeu de funil nenhum, e continua idêntico ao que o E1
# escreveu.


# --- desfechos que não tocam --------------------------------------------------


def test_an_unsupported_event_type_is_discarded(
    admin: psycopg.Connection, two_tenants: TwoTenants, number: ChannelAccount
) -> None:
    # Descartar ≠ falhar: reprocessar um tipo sem handler não mudaria o
    # resultado, então o evento leva rastro (`discarded`) e não a escada de
    # retry até uma DLQ que nunca mudaria de ideia.
    #
    # O exemplo era `order_paid` até o E3 S5, e escolhê-lo foi um erro de
    # engenharia: ele era o caso POSITIVO do passo seguinte deste mesmo marco,
    # então o dia em que o pagamento ganhou handler este teste passou a afirmar
    # o contrário do produto. O invariante ("tipo sem handler é descartado, com
    # rastro, sem tocar em nada") continua idêntico; só a ilustração foi
    # trocada, com autorização explícita do Bruno.
    #
    # Regra adotada no marco a partir daqui: **exemplo de caso negativo nunca é
    # o caso positivo do passo seguinte.** `theme_published` é um tópico real da
    # Shopify e não está na fila de nenhum passo do E0 ao E8 — nada neste produto
    # vai reagir a um tema publicado.
    event_id = abandonment(admin, two_tenants.a.id, event_type="theme_published")

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
#
# `test_the_touch_is_not_executable_by_everyone` vivia aqui e afirmava o EXECUTE
# mínimo (ADR-11) sobre a assinatura de DUAS casas — o shim N-1, removido na
# contração do S10. O invariante não mudou e não ficou sem prova: ele está,
# palavra por palavra e sobre a assinatura que sobrou, em
# `tests/db/test_start_funnel_run.py::test_neither_function_is_executable_by_everyone`,
# que percorre os mesmos três papéis (`authenticated`, `sender_role`,
# `ingestion_role`) contra `internal.apply_domain_event(1)` e contra
# `internal.start_funnel_run(...)`. Reescrevê-lo aqui apontando para a mesma
# chamada seria uma segunda cópia da mesma afirmação: duas provas para um
# invariante não o provam duas vezes, só dobram o custo de mudá-lo.

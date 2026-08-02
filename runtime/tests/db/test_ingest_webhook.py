"""A1 — `internal.ingest_webhook()`, a porta de entrada do motor.

Uma chamada, uma transação, dois ramos. O que esta suíte afirma é o contrato
transacional da função: atomicidade, idempotência, de quem é o dado, e o que
cada ramo faz — **não** o formato do payload, que é da Edge Function que a
chama, com schema estrito, e chega junto com ela.

Os testes rodam como `postgres` porque a função é `security definer` e o papel
que a executará em produção ainda não está fiado (pendência registrada na
migration). O que se prova aqui é o comportamento; quem pode chamar é assunto
do teste de privilégio, no fim do arquivo.
"""


import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.db.conftest import TwoTenants
from tests.db.factories import (
    ChannelAccount,
    ConnectorAccount,
    create_channel_account,
    create_connector_account,
    unique_id,
    unique_phone,
)


def ingest(
    conn: psycopg.Connection,
    *,
    source: str,
    source_account_id: str,
    external_event_id: str | None = None,
    event_type: str = "checkout_abandoned",
    payload: dict | None = None,
) -> tuple:
    """Chama a função e devolve o outcome como tupla (status, event_id, tenant, conversa, seq)."""
    row = conn.execute(
        "select * from internal.ingest_webhook(%s, %s, %s, %s, %s)",
        (
            source,
            source_account_id,
            external_event_id or unique_id("evt"),
            event_type,
            Jsonb(payload or {}),
        ),
    ).fetchone()
    return row


def queue_length(conn: psycopg.Connection, queue: str) -> int:
    row = conn.execute("select queue_length from pgmq.metrics(%s)", (queue,)).fetchone()
    return int(row[0])


@pytest.fixture
def store(admin: psycopg.Connection, two_tenants: TwoTenants) -> ConnectorAccount:
    return create_connector_account(admin, two_tenants.a.id)


@pytest.fixture
def number(admin: psycopg.Connection, two_tenants: TwoTenants) -> ChannelAccount:
    return create_channel_account(admin, two_tenants.a.id)


@pytest.fixture(autouse=True)
def empty_queues(admin: psycopg.Connection):
    admin.execute("select pgmq.purge_queue('q_domain_events')")
    admin.execute("select pgmq.purge_queue('q_inbound')")
    yield
    admin.execute("select pgmq.purge_queue('q_domain_events')")
    admin.execute("select pgmq.purge_queue('q_inbound')")


# --- 1 · atomicidade --------------------------------------------------------


def test_an_error_halfway_through_persists_nothing(
    admin: psycopg.Connection, number: ChannelAccount
) -> None:
    """A asserção mais importante da suíte.

    Um telefone fora do E.164 faz o INSERT de contato explodir — depois do
    evento já ter sido gravado. Ou a transação inteira volta, ou fica um evento
    órfão marcado como recebido que ninguém jamais processa: o webhook some, e
    o cliente fica sem resposta sem que nada apareça em lugar nenhum.
    """
    external_event_id = unique_id("evt")

    with pytest.raises(psycopg.errors.CheckViolation):
        ingest(
            admin,
            source="meta",
            source_account_id=number.external_account_id,
            external_event_id=external_event_id,
            event_type="message_inbound",
            payload={"from": "não é um telefone"},
        )

    admin.rollback()

    orphans = admin.execute(
        "select count(*) from internal.webhook_events where external_event_id = %s",
        (external_event_id,),
    ).fetchone()[0]

    assert orphans == 0


# --- 2 e 4 · idempotência ---------------------------------------------------


def test_reprocessing_three_times_produces_exactly_one_effect(
    admin: psycopg.Connection, store: ConnectorAccount
) -> None:
    # Plataformas reenviam. A reconciliação por poll chama a mesma função. Se a
    # terceira entrega criasse um terceiro evento, o funil dispararia três vezes
    # para o mesmo abandono.
    external_event_id = unique_id("evt")
    outcomes = [
        ingest(
            admin,
            source="shopify",
            source_account_id=store.source_account_id,
            external_event_id=external_event_id,
        )
        for _ in range(3)
    ]

    assert [outcome[0] for outcome in outcomes] == ["ingested", "duplicate", "duplicate"]

    events = admin.execute(
        "select count(*) from internal.webhook_events where external_event_id = %s",
        (external_event_id,),
    ).fetchone()[0]

    assert events == 1
    assert queue_length(admin, "q_domain_events") == 1


def test_the_duplicate_points_at_the_event_that_already_existed(
    admin: psycopg.Connection, store: ConnectorAccount
) -> None:
    # Devolver o id do evento original é o que permite ao chamador responder
    # 200 com rastro em vez de 200 no escuro.
    external_event_id = unique_id("evt")
    first = ingest(
        admin,
        source="shopify",
        source_account_id=store.source_account_id,
        external_event_id=external_event_id,
    )
    second = ingest(
        admin,
        source="shopify",
        source_account_id=store.source_account_id,
        external_event_id=external_event_id,
    )

    assert second[0] == "duplicate"
    assert second[1] == first[1]


# --- 3 · a colisão entre lojas ----------------------------------------------


def test_the_same_event_id_in_two_stores_produces_two_events(
    admin: psycopg.Connection, two_tenants: TwoTenants
) -> None:
    """Plataformas dão ids sequenciais POR LOJA.

    Sem `source_account_id` na chave, o evento "1042" da segunda loja seria
    engolido como duplicata do "1042" da primeira — e o segundo lojista nunca
    saberia por que o abandono dele não virou conversa.
    """
    first_store = create_connector_account(admin, two_tenants.a.id)
    second_store = create_connector_account(admin, two_tenants.b.id)
    shared_event_id = "1042"

    first = ingest(
        admin,
        source="shopify",
        source_account_id=first_store.source_account_id,
        external_event_id=shared_event_id,
    )
    second = ingest(
        admin,
        source="shopify",
        source_account_id=second_store.source_account_id,
        external_event_id=shared_event_id,
    )

    assert [first[0], second[0]] == ["ingested", "ingested"]
    assert first[1] != second[1]
    assert first[2] == two_tenants.a.id
    assert second[2] == two_tenants.b.id


# --- 5 · de quem é o dado ---------------------------------------------------


def test_the_tenant_comes_from_the_source_account_not_from_the_payload(
    admin: psycopg.Connection, two_tenants: TwoTenants, store: ConnectorAccount
) -> None:
    # A fronteira de confiança, no ponto onde ela é mais fácil de furar: o
    # webhook é dado de terceiro, e dado de terceiro não escolhe de quem é a
    # conversa.
    outcome = ingest(
        admin,
        source="shopify",
        source_account_id=store.source_account_id,
        payload={"tenant_id": str(two_tenants.b.id), "total": 199},
    )

    assert outcome[2] == two_tenants.a.id


def test_an_unknown_account_writes_nothing(admin: psycopg.Connection) -> None:
    external_event_id = unique_id("evt")

    outcome = ingest(
        admin,
        source="shopify",
        source_account_id="loja-que-nunca-existiu",
        external_event_id=external_event_id,
    )

    assert outcome[0] == "unknown_account"
    assert (
        admin.execute(
            "select count(*) from internal.webhook_events where external_event_id = %s",
            (external_event_id,),
        ).fetchone()[0]
        == 0
    )


# --- 6 · o ramo de canal ----------------------------------------------------


def test_an_inbound_message_is_stored_with_its_sequence_and_a_deadline(
    admin: psycopg.Connection, number: ChannelAccount
) -> None:
    phone = unique_phone()

    outcome = ingest(
        admin,
        source="meta",
        source_account_id=number.external_account_id,
        event_type="message_inbound",
        payload={"from": phone, "message": {"text": "meu pedido não chegou"}},
    )

    assert outcome[0] == "ingested"
    conversation_id, seq = outcome[3], outcome[4]
    assert seq == 1

    stored = admin.execute(
        """
        select m.direction, m.seq, m.author_type, c.pending_response_at is not null
          from public.messages m
          join public.conversations c on c.id = m.conversation_id
         where m.conversation_id = %s
        """,
        (conversation_id,),
    ).fetchall()

    assert stored == [("inbound", 1, "contact", True)]


def test_an_inbound_message_does_not_enqueue_anything(
    admin: psycopg.Connection, number: ChannelAccount
) -> None:
    """O invariante que a v1.2 da arquitetura comprou eliminando o duplo enfileiramento.

    Quem cria job de entrada é o coalescer, numa transação só, com o contador de
    geração. A ingestão só marca o prazo. Enfileirar aqui traria de volta o job
    por mensagem que a rajada de cinco multiplicava por cinco.
    """
    ingest(
        admin,
        source="meta",
        source_account_id=number.external_account_id,
        event_type="message_inbound",
        payload={"from": unique_phone(), "message": {"text": "oi"}},
    )

    assert queue_length(admin, "q_inbound") == 0


def test_a_second_message_from_the_same_contact_reuses_the_conversation(
    admin: psycopg.Connection, number: ChannelAccount
) -> None:
    phone = unique_phone()
    first = ingest(
        admin,
        source="meta",
        source_account_id=number.external_account_id,
        event_type="message_inbound",
        payload={"from": phone, "message": {"text": "oi"}},
    )
    second = ingest(
        admin,
        source="meta",
        source_account_id=number.external_account_id,
        event_type="message_inbound",
        payload={"from": phone, "message": {"text": "alguém aí?"}},
    )

    assert second[3] == first[3]
    assert [first[4], second[4]] == [1, 2]


# --- 7 · o ramo de plataforma -----------------------------------------------


def test_a_platform_event_is_enqueued_for_the_domain_worker(
    admin: psycopg.Connection, store: ConnectorAccount
) -> None:
    outcome = ingest(
        admin,
        source="shopify",
        source_account_id=store.source_account_id,
        event_type="checkout_abandoned",
    )

    assert queue_length(admin, "q_domain_events") == 1

    job = admin.execute(
        "select message from pgmq.read('q_domain_events', 5, 1)"
    ).fetchone()[0]

    assert job == {"webhook_event_id": outcome[1]}

    status = admin.execute(
        "select status from internal.webhook_events where id = %s", (outcome[1],)
    ).fetchone()[0]

    assert status == "enqueued"


# --- quem pode chamar -------------------------------------------------------


def test_the_ingestion_is_not_executable_by_everyone(dsn: str) -> None:
    # A função escreve atravessando tenants; quem pode executá-la tem a chave da
    # ingestão. EXECUTE revogado de PUBLIC é o que o ADR-11 exige de toda função
    # `security definer`.
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("set role authenticated")

            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "select * from internal.ingest_webhook("
                    "'shopify', 'x', 'y', 'z', '{}'::jsonb)"
                )


def test_the_worker_cannot_ingest_either(dsn: str) -> None:
    # Deliberado: o worker não é a ingestão. Quando a reconciliação por poll
    # existir (ela chama a mesma função), o grant vira uma decisão consciente e
    # não uma herança.
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("set role worker_role")

            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "select * from internal.ingest_webhook("
                    "'shopify', 'x', 'y', 'z', '{}'::jsonb)"
                )

"""E3 · S11 — o contexto de quem criou o trabalho chega em quem faz o trabalho.

O slot `otel` existe nos jobs desde o E1 e as quatro classes o leem. O que nunca
existiu foi um PRODUTOR: `dispatch_pass` montava o payload da `q_scheduled` com
dois ids, e `internal.ingest_webhook` — produtor de TODO job da
`q_domain_events`, inclusive os da reconciliação — montava o dele com um id só. O
contexto morria na porta, e um slot que só é lido é um slot que vai chegar vazio
no dia em que o Logfire existir: cada span nasce raiz, o tique e o trabalho que
ele causou viram dois traces sem relação, e o exportador que custou a credencial
não compra nada.

Estes testes cobram a travessia real, pelo pgmq, nas duas filas onde este marco
criou trabalho novo. **Não há exportador aqui**, nem SDK, nem endpoint OTLP:
isso depende do Logfire e do Grafana Cloud (pendências B-2/B-3). O que está aqui
é a propriedade que, faltando, torna o exportador inútil quando chegar.
"""

import json
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from agents_runtime.connectors.port import PlatformEvent
from agents_runtime.connectors.reconcile import reconcile_pass
from agents_runtime.obs import context
from agents_runtime.queueing import DOMAIN_EVENTS, SCHEDULED
from agents_runtime.queueing.dispatcher import dispatch_pass
from agents_runtime.queueing.jobs import DomainEventJob, ScheduledTouchJob
from tests.db.conftest import TwoTenants
from tests.db.factories import (
    ConnectorAccount,
    create_connector_account,
    create_thread,
    unique_id,
    unique_phone,
)
from tests.db.factories_e3 import create_funnel, create_scheduled_touch
from tests.support.fake_connector import ScriptedConnector

pytestmark = pytest.mark.db

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
ALWAYS = timedelta(0)


def a_context() -> context.Carrier | None:
    """O que uma `TraceSource` instrumentada devolverá quando B-2/B-3 chegar."""
    return context.carrier(TRACEPARENT)


def drain(conn: psycopg.Connection, queue: str) -> list[dict]:
    return [
        row[0] if isinstance(row[0], dict) else json.loads(row[0])
        for row in conn.execute("select message from pgmq.read(%s, 30, 100)", (queue,)).fetchall()
    ]


def a_due_touch(conn: psycopg.Connection, tenant_id) -> None:
    thread = create_thread(conn, tenant_id)
    funnel = create_funnel(conn, tenant_id)
    create_scheduled_touch(
        conn, tenant_id, funnel.id, thread.contact_id, conversation_id=thread.conversation_id
    )


def an_abandonment() -> PlatformEvent:
    """Um evento na forma que a Edge Function entrega — forma, não semelhança."""
    external_id = unique_id("evt")
    return PlatformEvent(
        external_event_id=external_id,
        event_type="checkout_abandoned",
        occurred_at=datetime.now(UTC),
        payload={"phone": unique_phone(), "order": {"external_id": f"ord-{external_id}"}},
    )


def events_of(conn: psycopg.Connection, store: ConnectorAccount) -> set[int]:
    return {
        row[0]
        for row in conn.execute(
            "select id from internal.webhook_events where source_account_id = %s",
            (store.source_account_id,),
        ).fetchall()
    }


class TestAFilaDosToques:
    async def test_o_job_carrega_o_contexto_do_tique_que_o_criou(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        a_due_touch(admin, two_tenants.a.id)

        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            assert await dispatch_pass(conn, trace=a_context) >= 1

        (payload,) = [
            message
            for message in drain(admin, SCHEDULED)
            if message.get("tenant_id") == str(two_tenants.a.id)
        ]
        assert ScheduledTouchJob.from_payload(payload).otel == {"traceparent": TRACEPARENT}

    async def test_sem_contexto_o_job_e_o_de_sempre(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # O default da composição hoje, e a trava do S4: só ids viajam. Um
        # `"otel": null` fixo passaria pela trava e sujaria toda a fila com uma
        # chave que não afirma nada.
        a_due_touch(admin, two_tenants.a.id)

        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            await dispatch_pass(conn)

        (payload,) = [
            message
            for message in drain(admin, SCHEDULED)
            if message.get("tenant_id") == str(two_tenants.a.id)
        ]
        assert set(payload) == {"scheduled_touch_id", "tenant_id"}


class TestAVarreduraDeReconciliacao:
    """O poll entra pela mesma porta do webhook (D5) — e agora leva o contexto.

    Importa mais aqui do que no webhook, e é a D5 que explica: os dois entram
    pela mesma função, mas o trace do webhook começa fora do nosso processo e o
    desta varredura começa num tique nosso. Sem a travessia, "a loja X foi
    reconciliada" e "o funil do pedido Y morreu porque ele estava pago" são dois
    traces contando uma história só.
    """

    async def test_o_job_de_dominio_nasce_com_o_contexto_do_passe(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        store = create_connector_account(admin, two_tenants.a.id)
        connector = ScriptedConnector([an_abandonment()])

        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            await conn.execute("set role ingestion_role")
            result = await reconcile_pass(
                conn, {"shopify": connector}, stale_after=ALWAYS, trace=a_context
            )

        assert result.ingested == 1
        mine = events_of(admin, store)
        (payload,) = [
            message
            for message in drain(admin, DOMAIN_EVENTS)
            if message.get("webhook_event_id") in mine
        ]
        assert DomainEventJob.from_payload(payload).otel == {"traceparent": TRACEPARENT}

    async def test_sem_contexto_o_job_de_dominio_continua_so_com_o_id(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # `jsonb_strip_nulls` na função: um `"otel": null` em todo job da fila
        # seria uma chave que não afirma nada, gravada para sempre.
        store = create_connector_account(admin, two_tenants.a.id)
        connector = ScriptedConnector([an_abandonment()])

        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            await conn.execute("set role ingestion_role")
            await reconcile_pass(conn, {"shopify": connector}, stale_after=ALWAYS)

        mine = events_of(admin, store)
        (payload,) = [
            message
            for message in drain(admin, DOMAIN_EVENTS)
            if message.get("webhook_event_id") in mine
        ]
        assert set(payload) == {"webhook_event_id"}

    def test_a_chamada_de_cinco_argumentos_continua_valendo(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # Compatibilidade N-1, e a razão de o parâmetro ser o ÚLTIMO e ter
        # default: a Edge Function chama por nome e aridade, com cinco
        # argumentos. Um parâmetro no meio teria trocado o significado
        # posicional dos que já existiam; e `create or replace` (em vez do DROP)
        # teria criado uma SEGUNDA função, deixando esta chamada ambígua com um
        # "function is not unique" em produção.
        store = create_connector_account(admin, two_tenants.a.id)

        status = admin.execute(
            "select status from internal.ingest_webhook(%s, %s, %s, %s, %s)",
            (
                "shopify",
                store.source_account_id,
                unique_id("evt"),
                "order_paid",
                psycopg.types.json.Jsonb({}),
            ),
        ).fetchone()[0]

        assert status == "ingested"

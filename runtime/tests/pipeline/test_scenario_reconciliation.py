"""O evento que o webhook perdeu chega ao contato mesmo assim — o cinto do ADR-3.

`test_scenario_funnel_dispatch` prova o fio inteiro a partir de um webhook. Este
prova o MESMO fio a partir de nada: nenhuma chamada de ingestão é feita pelo
teste, nenhuma linha é plantada. A entrega falhou — a plataforma não reentregou,
ou a nossa Edge Function respondeu 500 num deploy — e a única coisa que sabe do
abandono é a loja, quando alguém pergunta.

    (nada) → poll de 15 min → a MESMA ingest_webhook → q_domain_events →
    apply_domain_event → start_funnel_run → scheduled_touches →
    claim_due_touches → q_scheduled → a escada → o CAS → message_outbox →
    sender → o canal

A asserção é que esse caminho não tem NENHUM passo próprio. O poll traduz e
bate na porta que já existia (D5); tudo depois dele é o código que o webhook já
exercitava, sem um ramo "veio do poll" em lugar nenhum — porque um ramo desses
seria um segundo comportamento para manter em dia, e ninguém descobriria que os
dois divergiram até um cliente receber a mesma mensagem duas vezes.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from agents_runtime.app import run
from agents_runtime.config import QueueingConfig
from agents_runtime.connectors.port import PlatformEvent
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
from tests.support.fake_connector import ScriptedConnector

pytestmark = pytest.mark.pipeline

CADENCE = [{"n": 1, "delay": "PT0S", "copy_base": "Vi que ficou algo no carrinho."}]


async def test_only_the_poll_ever_saw_it_and_the_contact_still_hears_from_us(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
    tiny_config: QueueingConfig,
) -> None:
    tenant_id = create_tenant(sync_admin)
    store = create_connector_account(sync_admin, tenant_id)
    create_channel_account(sync_admin, tenant_id)
    create_funnel(sync_admin, tenant_id, touches=CADENCE)
    phone = unique_phone()

    lost_delivery = PlatformEvent(
        external_event_id=unique_id("evt"),
        event_type="checkout_abandoned",
        occurred_at=datetime.now(UTC),
        payload={"phone": phone},
    )
    connector = ScriptedConnector([lost_delivery])

    # `stale_after=0` makes every store due on every tick, which is what turns
    # a quarter of an hour into fifty milliseconds without the sweep knowing it
    # is being tested. The repeat is free and says so: D5 answers `duplicate`.
    config = replace(
        tiny_config,
        dispatcher_tick=timedelta(milliseconds=50),
        reconcile_tick=timedelta(milliseconds=50),
        reconcile_stale_after=timedelta(0),
    )

    stop = asyncio.Event()
    running = asyncio.create_task(
        run(
            dsn,
            stop=stop,
            config=config,
            channel=FakeChannel(dsn),
            connectors={"shopify": connector},
            worker_set_role="worker_role",
            reconcile_set_role="ingestion_role",
            sender_set_role="sender_role",
        )
    )
    try:

        async def delivered():
            cursor = await admin.execute(
                "select payload, to_phone_e164 from testing.fake_channel_sends"
            )
            return await cursor.fetchone()

        send = await eventually(
            delivered, note="o toque nascido de um evento que só o poll viu"
        )
        assert send[1] == phone
        assert CADENCE[0]["copy_base"] in send[0]["text"]

        # E exatamente um. O tique repetiu o poll dezenas de vezes enquanto o
        # funil corria; se cada passagem tivesse virado um efeito, o contato
        # teria recebido a mesma abordagem uma vez por tique.
        cursor = await admin.execute("select count(*) from testing.fake_channel_sends")
        assert (await cursor.fetchone())[0] == 1

        cursor = await admin.execute(
            """
            select count(*), min(connector_account_id::text)
              from internal.webhook_events
             where external_event_id = %s
            """,
            (lost_delivery.external_event_id,),
        )
        rows, connector_account = await cursor.fetchone()
        assert rows == 1
        # A dívida do S5 fechada na prática: a loja foi resolvida NA INGESTÃO,
        # pelo caminho do poll, exatamente como pelo do webhook.
        assert connector_account == str(store.id)
    finally:
        stop.set()
        await asyncio.wait_for(running, timeout=DEADLINE)

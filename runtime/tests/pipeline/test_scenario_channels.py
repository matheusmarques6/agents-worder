"""E3 · S7 — os dois canais com o motor de verdade rodando.

Três provas, e nenhuma delas cabe num unitário porque nenhuma delas é sobre uma
regra: são sobre a regra **estar no caminho**.

1. **A variação anti-ban existe, e o validador determinístico manda.** Um toque
   por Evolution sai reescrito e marcado `generated: true`; um toque cuja
   variação inventa um número que a base não tinha **não sai**, e abre linha em
   `alerts`. É o único ponto do produto em que texto de LLM chega a um contato
   sem portão de LLM (D3) — então a prova de que o portão está lá é o cenário,
   não o unitário.

2. **O tier a 80% pausa proativo e NÃO pausa reativa.** As duas metades no mesmo
   teste, de propósito: separadas, alguém "conserta" a pausa e só a metade que
   não protege ninguém reprova. Pausar resposta a cliente é o erro que mata
   atendimento.

3. **`funnels.channel_preference` escolhe a saída.** A coluna existe desde o S2 e
   ninguém a lia.

A composição é a real (`app.run`), os papéis são os de produção, e o único dublê
é o que custaria dinheiro ou um número de telefone.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agents_runtime.app import run
from agents_runtime.config import QueueingConfig
from tests.db.factories import (
    create_channel_account,
    create_connector_account,
    create_tenant,
    create_thread,
    unique_id,
    unique_phone,
)
from tests.db.factories_e3 import create_funnel
from tests.pipeline.test_scenarios_a import DEADLINE, eventually
from tests.support.fake_channel import FakeChannel

BASE_COPY = "Vi que ficou algo no carrinho."
CADENCE = [{"n": 1, "delay": "PT0S", "copy_base": BASE_COPY}]


def variator_saying(*replies: str):
    """O dublê da variação. Não há chave de LLM aqui, e não deveria haver."""
    pending = list(replies)

    async def variate(base: str, *, previous: str | None) -> str:
        return pending.pop(0) if pending else replies[-1]

    return variate


async def drive(dsn: str, tiny_config: QueueingConfig, until, *, variator=None, note=""):
    """Roda o processo real até `until` responder, e para."""
    config = replace(tiny_config, dispatcher_tick=timedelta(milliseconds=50))
    stop = asyncio.Event()
    running = asyncio.create_task(
        run(
            dsn,
            stop=stop,
            config=config,
            channel=FakeChannel(dsn),
            variator=variator,
            worker_set_role="worker_role",
            sender_set_role="sender_role",
        )
    )
    try:
        return await eventually(until, note=note)
    finally:
        stop.set()
        await asyncio.wait_for(running, DEADLINE)


async def abandon(sync_admin: psycopg.Connection, store, phone: str) -> None:
    sync_admin.execute(
        "select * from internal.ingest_webhook('shopify', %s, %s, 'checkout_abandoned', %s)",
        (store.source_account_id, unique_id("evt"), Jsonb({"phone": phone})),
    )


def evolution_number(sync_admin: psycopg.Connection, tenant_id, *, risk_accepted: bool = True):
    """Um número Evolution pronto para disparar.

    O aceite do risco é parte do setup porque ele é parte do produto: canal não
    oficial só dispara depois que o lojista assumiu o risco de banimento, por
    escrito e com trilha. `TestNothingLeavesBeforeTheRiskIsAccepted` é a prova de
    que isso não é decoração.
    """
    account = create_channel_account(sync_admin, tenant_id, type="evolution")
    if risk_accepted:
        sync_admin.execute("select internal.accept_channel_risk(%s)", (account.id,))
    return account


def consented(sync_admin: psycopg.Connection, tenant_id, phone: str) -> None:
    """Um contato que já autorizou.

    Necessário em todo cenário Evolution: o RF-033(a) manda o primeiro toque a
    um contato `pending` carregar os botões Autorizar/Bloquear, e o adaptador da
    Evolution RECUSA um toque com botões — aquele par não tem forma confiável
    nem caminho de volta neste canal (pendência nomeada do S7). Sem o
    consentimento prévio, o que este arquivo estaria medindo era a recusa, não a
    variação.
    """
    sync_admin.execute(
        "insert into public.contacts (tenant_id, phone_e164, opt_status)"
        " values (%s, %s, 'authorized')",
        (tenant_id, phone),
    )


class TestTheCopyVariesAndTheGateHolds:
    async def test_an_evolution_touch_leaves_rewritten_and_marked_as_generated(
        self,
        dsn: str,
        admin: psycopg.AsyncConnection,
        sync_admin: psycopg.Connection,
        tiny_config: QueueingConfig,
    ) -> None:
        tenant_id = create_tenant(sync_admin)
        store = create_connector_account(sync_admin, tenant_id)
        evolution_number(sync_admin, tenant_id)
        create_funnel(sync_admin, tenant_id, touches=CADENCE, channel_preference="evolution")
        phone = unique_phone()
        consented(sync_admin, tenant_id, phone)
        await abandon(sync_admin, store, phone)

        async def delivered():
            cursor = await admin.execute("select payload from testing.fake_channel_sends")
            return await cursor.fetchone()

        (payload,) = await drive(
            dsn,
            tiny_config,
            delivered,
            variator=variator_saying("Passei pra lembrar do seu carrinho."),
            note="o toque variado chegando ao canal",
        )

        # A copy é outra, e o item diz que foi um modelo que a escreveu (D3c). A
        # auditoria que separa copy gerada de template aprovado não pode ser
        # escrita retroativamente.
        assert payload["text"] == "Passei pra lembrar do seu carrinho."
        assert payload["generated"] is True

    async def test_a_variation_that_invents_a_number_never_reaches_the_contact(
        self,
        dsn: str,
        admin: psycopg.AsyncConnection,
        sync_admin: psycopg.Connection,
        tiny_config: QueueingConfig,
    ) -> None:
        # O Judge 1 não olha disparo (D3). Quem segura é o validador
        # determinístico — e o que ele segura é um preço que a loja nunca cotou
        # chegando ao WhatsApp de uma pessoa.
        tenant_id = create_tenant(sync_admin)
        store = create_connector_account(sync_admin, tenant_id)
        evolution_number(sync_admin, tenant_id)
        create_funnel(sync_admin, tenant_id, touches=CADENCE, channel_preference="evolution")
        phone = unique_phone()
        consented(sync_admin, tenant_id, phone)
        await abandon(sync_admin, store, phone)

        async def alerted():
            cursor = await admin.execute(
                "select type, severity, payload->'violations' from public.alerts"
                " where type = 'critical_violation'"
            )
            return await cursor.fetchone()

        alert = await drive(
            dsn,
            tiny_config,
            alerted,
            variator=variator_saying(
                "Fecho por R$ 49,90 pra você!",
                "Só hoje: R$ 39,90!",
            ),
            note="o alerta da variação barrada",
        )

        assert alert[0] == "critical_violation"
        assert alert[1] == "critical"
        assert "introduced_number" in alert[2]

        # E o que importa: NADA saiu, e nenhuma linha de outbox foi escrita.
        silence = await (
            await admin.execute(
                "select (select count(*) from testing.fake_channel_sends)"
                " + (select count(*) from internal.message_outbox)"
            )
        ).fetchone()
        assert silence == (0,)


class TestNothingLeavesBeforeTheRiskIsAccepted:
    async def test_an_evolution_number_without_the_acceptance_sends_nothing(
        self,
        dsn: str,
        admin: psycopg.AsyncConnection,
        sync_admin: psycopg.Connection,
        tiny_config: QueueingConfig,
    ) -> None:
        # Descoberto ao escrever este arquivo, e não por dedução: o primeiro
        # cenário de Evolution não entregou nada, porque o número não tinha
        # aceite. A trava estava no caminho antes de existir um teste dizendo
        # que estava.
        #
        # E a linha NÃO falha: ela volta para `pending`. Um aceite pendente é
        # uma pendência administrativa, não um erro de envio — no dia em que o
        # lojista assinar, o toque sai sem que ninguém precise reprocessar nada.
        tenant_id = create_tenant(sync_admin)
        store = create_connector_account(sync_admin, tenant_id)
        evolution_number(sync_admin, tenant_id, risk_accepted=False)
        create_funnel(sync_admin, tenant_id, touches=CADENCE, channel_preference="evolution")
        phone = unique_phone()
        consented(sync_admin, tenant_id, phone)
        await abandon(sync_admin, store, phone)

        async def held():
            # `next_attempt_at` no futuro é o fato que distingue "foi
            # reivindicada e devolvida" de "ainda nem foi olhada" — sem ele este
            # teste passaria com um sender parado.
            cursor = await admin.execute(
                "select status, attempt_count from internal.message_outbox"
                " where status = 'pending' and next_attempt_at > now()"
            )
            return await cursor.fetchone()

        row = await drive(
            dsn,
            tiny_config,
            held,
            variator=variator_saying("Passei pra lembrar do seu carrinho."),
            note="a linha de outbox escrita e segurada",
        )

        # Zero tentativas: adiar devolveu a que o claim tinha gasto.
        assert row == ("pending", 0)

        delivered = await (
            await admin.execute("select count(*) from testing.fake_channel_sends")
        ).fetchone()
        assert delivered == (0,)


class TestTheTierPausesProactivesAndOnlyProactives:
    async def test_at_eighty_percent_the_touch_stops_and_the_reply_still_goes(
        self,
        dsn: str,
        admin: psycopg.AsyncConnection,
        sync_admin: psycopg.Connection,
        tiny_config: QueueingConfig,
    ) -> None:
        # As duas metades no MESMO teste. Separadas, alguém relaxa a pausa e só
        # a metade que não protege ninguém reprova.
        tenant_id = create_tenant(sync_admin)
        store = create_connector_account(sync_admin, tenant_id)
        account = create_channel_account(sync_admin, tenant_id)
        create_funnel(sync_admin, tenant_id, touches=CADENCE)
        sync_admin.execute(
            "update public.channels_accounts set meta_tier = 10, tier_usage_24h = 8"
            " where id = %s",
            (account.id,),
        )

        phone = unique_phone()
        await abandon(sync_admin, store, phone)

        # A reativa: uma linha de outbox que o sender vai drenar de qualquer
        # jeito. Ela representa o contato que escreveu e está esperando.
        reply_thread = create_thread(sync_admin, tenant_id, channel_account_id=account.id)
        sync_admin.execute(
            """
            insert into internal.message_outbox
                (tenant_id, conversation_id, contact_id, channel_account_id,
                 kind, payload, idempotency_key)
            values (%s, %s, %s, %s, 'reply', %s, %s)
            """,
            (
                tenant_id,
                reply_thread.conversation_id,
                reply_thread.contact_id,
                account.id,
                Jsonb({"text": "Chega quinta-feira."}),
                unique_id("idem"),
            ),
        )

        async def cancelled():
            cursor = await admin.execute(
                "select cancel_reason from public.scheduled_touches where status = 'cancelled'"
            )
            return await cursor.fetchone()

        reason = await drive(
            dsn, tiny_config, cancelled, note="o toque parado pelo tier"
        )

        assert reason == ("channel_paused_tier",)

        # A metade que mata atendimento se quebrar: a resposta saiu.
        delivered = await (
            await admin.execute("select payload->>'text' from testing.fake_channel_sends")
        ).fetchall()

        assert delivered == [("Chega quinta-feira.",)]


class TestThePreferenceChoosesTheWayOut:
    @pytest.mark.parametrize("preference", ["cloud", "evolution"])
    async def test_the_funnel_leaves_by_the_channel_it_asked_for(
        self,
        dsn: str,
        admin: psycopg.AsyncConnection,
        sync_admin: psycopg.Connection,
        preference: str,
    ) -> None:
        # As duas contas existem e ativas; a mais nova é sempre a `evolution`,
        # que era a que o roteamento antigo escolhia sozinho. Se a preferência
        # não fosse lida, o caso `cloud` sairia pela conta errada.
        tenant_id = create_tenant(sync_admin)
        store = create_connector_account(sync_admin, tenant_id)
        cloud = create_channel_account(sync_admin, tenant_id, type="cloud")
        evolution = create_channel_account(sync_admin, tenant_id, type="evolution")
        create_funnel(
            sync_admin, tenant_id, touches=CADENCE, channel_preference=preference
        )
        phone = unique_phone()
        await abandon(sync_admin, store, phone)

        event_id = sync_admin.execute(
            "select id from internal.webhook_events order by id desc limit 1"
        ).fetchone()[0]
        sync_admin.execute("select * from internal.apply_domain_event(%s)", (event_id,))

        chosen = sync_admin.execute(
            "select ca.type from public.conversations c"
            "  join public.channels_accounts ca on ca.id = c.channel_account_id"
            " where c.tenant_id = %s",
            (tenant_id,),
        ).fetchone()[0]

        assert chosen == preference
        assert {cloud.id, evolution.id} >= {
            sync_admin.execute(
                "select channel_account_id from public.conversations where tenant_id = %s",
                (tenant_id,),
            ).fetchone()[0]
        }

    async def test_an_explicit_preference_never_falls_back_to_the_other_channel(
        self, dsn: str, sync_admin: psycopg.Connection
    ) -> None:
        # Um lojista que pediu Evolution e não tem número Evolution tem uma
        # conexão faltando — que é exatamente o que `no_channel` significa. Cair
        # para a Cloud em silêncio seria ignorar a configuração justamente no
        # caso em que ela foi escrita de propósito.
        tenant_id = create_tenant(sync_admin)
        store = create_connector_account(sync_admin, tenant_id)
        create_channel_account(sync_admin, tenant_id, type="cloud")
        create_funnel(
            sync_admin, tenant_id, touches=CADENCE, channel_preference="evolution"
        )
        await abandon(sync_admin, store, unique_phone())

        event_id = sync_admin.execute(
            "select id from internal.webhook_events order by id desc limit 1"
        ).fetchone()[0]
        outcome = sync_admin.execute(
            "select * from internal.apply_domain_event(%s)", (event_id,)
        ).fetchone()

        assert outcome[0] == "no_channel"
        assert (
            sync_admin.execute(
                "select count(*) from public.scheduled_touches where tenant_id = %s",
                (tenant_id,),
            ).fetchone()[0]
            == 0
        )

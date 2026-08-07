"""E3 · S11 — as três situações que mereciam alerta e não abriam nenhum.

O marco tem alerta desde o S7 para uma coisa só: a travessia dos 80% do tier do
Meta (`internal.record_channel_send`, RF-035), e ele é testado em
`test_channel_pacing.py`. As outras três falhas do marco eram **silenciosas**:

1. **toque preso em `enqueued`** — o achado do S4. `claim_due_touches` marca o
   toque `enqueued` e enfileira o job; se o job morrer na DLQ, o toque fica num
   estado que a varredura NUNCA mais olha (ela só pega `pending`). Nada em lugar
   nenhum diz que aquela mensagem não vai sair. A decisão do plano foi
   **alerta de idade, jamais um segundo relógio** — um relógio que reenviasse
   seria a duplicidade que o CAS do S4 existe para impedir, e por isso este
   arquivo cobra explicitamente que a varredura **não muda** o toque;
2. **número banido** — `channels_accounts.status = 'banned'`. A conversa morre
   inteira e o `dispatch_touch` continua escrevendo linha na outbox para um
   número que não fala;
3. **loja em erro persistente** — o S8 fecha a varredura em `sync_status =
   'error'` e ninguém olha. E "persistente" não pode ser medido por
   `last_sync_at`, porque `finish_sync` o move a cada passe: uma loja que falha
   de cinco em cinco minutos tem `last_sync_at` sempre fresco. Daí
   `sync_error_since`, que nasce nesta migration com o seu escritor.

**PII nunca.** Um alerta é lido fora do Postgres (hub, admin, e amanhã o
Grafana): ids e contadores, nunca telefone, nome ou conteúdo. Cada bloco abaixo
tem o seu teste de vazamento.
"""

import uuid
from datetime import timedelta

import psycopg
import pytest

from agents_runtime.queueing.health import health_pass
from tests.db.conftest import TwoTenants, as_app_role
from tests.db.factories import (
    Thread,
    create_channel_account,
    create_connector_account,
    create_thread,
)
from tests.db.factories_e3 import create_funnel, create_scheduled_touch

pytestmark = pytest.mark.db

#: Os prazos que a `QueueingConfig` carrega. Escritos aqui como valores de teste
#: e não importados: o que este arquivo cobra é a função SQL, e ela recebe os
#: dois como PARÂMETRO justamente para não ter cópia própria de número nenhum.
STUCK_AFTER = timedelta(minutes=30)
SYNC_ERROR_AFTER = timedelta(hours=1)


def sweep(
    conn: psycopg.Connection,
    *,
    stuck_after: timedelta = STUCK_AFTER,
    sync_error_after: timedelta = SYNC_ERROR_AFTER,
) -> int:
    return conn.execute(
        "select internal.sweep_health_alerts(%s, %s)", (stuck_after, sync_error_after)
    ).fetchone()[0]


def alerts_of(conn: psycopg.Connection, tenant_id: uuid.UUID) -> list[tuple]:
    return conn.execute(
        "select type, severity, payload::text from public.alerts"
        "  where tenant_id = %s order by type",
        (tenant_id,),
    ).fetchall()


def stick(conn: psycopg.Connection, touch_id: uuid.UUID, *, minutes_ago: int) -> None:
    """O toque que a varredura reivindicou e o job nunca terminou.

    Escrito aqui, e não numa fábrica: o estado é exatamente o que
    `claim_due_touches` deixa (`enqueued` + `claimed_by` + `claimed_at`), e o
    envelhecimento é a única coisa que um teste não consegue esperar.
    """
    conn.execute(
        "update public.scheduled_touches"
        "   set status = 'enqueued', claimed_by = %s,"
        "       claimed_at = now() - make_interval(mins => %s)"
        " where id = %s",
        (uuid.uuid4(), minutes_ago, touch_id),
    )


@pytest.fixture
def thread(admin: psycopg.Connection, two_tenants: TwoTenants) -> Thread:
    return create_thread(admin, two_tenants.a.id)


def a_stuck_touch(
    admin: psycopg.Connection, tenant_id: uuid.UUID, thread: Thread, *, minutes_ago: int = 90
) -> uuid.UUID:
    funnel = create_funnel(admin, tenant_id)
    touch = create_scheduled_touch(
        admin, tenant_id, funnel.id, thread.contact_id, conversation_id=thread.conversation_id
    )
    stick(admin, touch, minutes_ago=minutes_ago)
    return touch


class TestOToquePresoEmEnqueued:
    def test_um_toque_parado_alem_do_prazo_abre_alerta(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        a_stuck_touch(admin, two_tenants.a.id, thread)

        assert sweep(admin) == 1

        (alert,) = alerts_of(admin, two_tenants.a.id)
        assert alert[0] == "touch_stuck"
        assert alert[1] == "warning"

    def test_um_toque_recem_reivindicado_nao_abre_nada(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        # Um toque na fila há dois minutos é trabalho em andamento. Alertar aqui
        # seria alertar sobre o funcionamento normal, que é como um alerta deixa
        # de ser lido.
        a_stuck_touch(admin, two_tenants.a.id, thread, minutes_ago=2)

        assert sweep(admin) == 0
        assert alerts_of(admin, two_tenants.a.id) == []

    def test_a_varredura_nao_encosta_no_toque(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        # A decisão do S4, literal: métrica/alerta de idade, NUNCA um segundo
        # relógio. Um relógio que devolvesse o toque para `pending` poderia
        # reenviar uma mensagem que já saiu — exatamente a duplicidade que o CAS
        # do S4 existe para impedir. A varredura observa e conta; quem conserta é
        # gente.
        touch = a_stuck_touch(admin, two_tenants.a.id, thread)
        before = admin.execute(
            "select status, claimed_by, claimed_at, cancel_reason, sent_at, outbox_id"
            "  from public.scheduled_touches where id = %s",
            (touch,),
        ).fetchone()

        sweep(admin)

        after = admin.execute(
            "select status, claimed_by, claimed_at, cancel_reason, sent_at, outbox_id"
            "  from public.scheduled_touches where id = %s",
            (touch,),
        ).fetchone()
        assert after == before
        assert after[0] == "enqueued"

    def test_o_alerta_conta_quantos_e_nomeia_o_mais_velho(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        # Um alerta por toque seria uma tempestade no dia em que a DLQ enche. O
        # alerta é de IDADE: quantos estão parados e desde quando o pior deles.
        # A lista completa é da view de contadores, não daqui.
        funnel = create_funnel(admin, two_tenants.a.id)
        oldest = create_scheduled_touch(
            admin,
            two_tenants.a.id,
            funnel.id,
            thread.contact_id,
            conversation_id=thread.conversation_id,
        )
        newer = create_scheduled_touch(
            admin,
            two_tenants.a.id,
            funnel.id,
            thread.contact_id,
            conversation_id=thread.conversation_id,
            touch_number=2,
        )
        stick(admin, oldest, minutes_ago=300)
        stick(admin, newer, minutes_ago=60)

        sweep(admin)

        payload = admin.execute(
            "select payload from public.alerts where type = 'touch_stuck'"
        ).fetchone()[0]
        assert payload["stuck_count"] == 2
        assert payload["oldest_scheduled_touch_id"] == str(oldest)
        assert payload["oldest_age_seconds"] >= 300 * 60

    def test_enquanto_o_alerta_estiver_aberto_a_varredura_nao_abre_outro(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        a_stuck_touch(admin, two_tenants.a.id, thread)

        assert sweep(admin) == 1
        assert sweep(admin) == 0
        assert len(alerts_of(admin, two_tenants.a.id)) == 1

    def test_resolvido_o_alerta_um_toque_ainda_preso_volta_a_avisar(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        # A deduplicação é por alerta ABERTO, não por alerta que existiu. Quem
        # marcou resolvido e não consertou tem de ser avisado de novo — senão a
        # primeira resolução apressada silencia o problema para sempre.
        a_stuck_touch(admin, two_tenants.a.id, thread)
        sweep(admin)
        admin.execute("update public.alerts set status = 'resolved'")

        assert sweep(admin) == 1

    def test_o_alerta_nao_carrega_pii(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        phone = admin.execute(
            "select phone_e164 from public.contacts where id = %s", (thread.contact_id,)
        ).fetchone()[0]
        a_stuck_touch(admin, two_tenants.a.id, thread)

        sweep(admin)

        payload, title = admin.execute(
            "select payload::text, title from public.alerts where type = 'touch_stuck'"
        ).fetchone()
        assert phone not in payload
        assert phone not in title
        assert str(thread.contact_id) not in payload

    def test_cada_tenant_recebe_o_seu(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # A varredura é cross-tenant por natureza (o molde de
        # `claim_due_touches`), e o alerta é do dono do toque. Um alerta no
        # tenant errado seria vazamento pela porta da observabilidade.
        for tenant in (two_tenants.a, two_tenants.b):
            a_stuck_touch(admin, tenant.id, create_thread(admin, tenant.id))

        assert sweep(admin) == 2
        assert len(alerts_of(admin, two_tenants.a.id)) == 1
        assert len(alerts_of(admin, two_tenants.b.id)) == 1


class TestONumeroBanido:
    def test_um_numero_banido_abre_alerta_critico(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        account = create_channel_account(admin, two_tenants.a.id)
        admin.execute(
            "update public.channels_accounts set status = 'banned' where id = %s", (account.id,)
        )

        assert sweep(admin) == 1

        (alert,) = alerts_of(admin, two_tenants.a.id)
        assert alert[0] == "channel_banned"
        # `critical` e não `warning`: um número banido não é um envio atrasado,
        # é a loja inteira muda naquele canal.
        assert alert[1] == "critical"

    def test_um_numero_ativo_nao_abre_nada(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        create_channel_account(admin, two_tenants.a.id)

        assert sweep(admin) == 0

    def test_o_alerta_e_por_numero_e_nao_se_repete(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        first = create_channel_account(admin, two_tenants.a.id)
        second = create_channel_account(admin, two_tenants.a.id, type="evolution")
        admin.execute("update public.channels_accounts set status = 'banned'")

        assert sweep(admin) == 2
        assert sweep(admin) == 0

        ids = {
            row[0]
            for row in admin.execute(
                "select payload->>'channel_account_id' from public.alerts"
                "  where type = 'channel_banned'"
            ).fetchall()
        }
        assert ids == {str(first.id), str(second.id)}

    def test_o_alerta_nao_carrega_o_telefone(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        account = create_channel_account(admin, two_tenants.a.id)
        admin.execute(
            "update public.channels_accounts set status = 'banned' where id = %s", (account.id,)
        )

        sweep(admin)

        payload, title = admin.execute(
            "select payload::text, title from public.alerts where type = 'channel_banned'"
        ).fetchone()
        assert account.phone_e164 not in payload
        assert account.phone_e164 not in title


class TestALojaEmErroPersistente:
    def test_finish_sync_carimba_desde_quando_a_loja_falha(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # A coluna nasce com escritor no mesmo commit: `last_sync_at` não serve
        # para medir persistência, porque `finish_sync` o move a cada passe.
        store = create_connector_account(admin, two_tenants.a.id)

        admin.execute("select internal.finish_sync(%s, 'error', null)", (store.id,))

        since = admin.execute(
            "select sync_error_since from public.connector_accounts where id = %s", (store.id,)
        ).fetchone()[0]
        assert since is not None

    def test_um_segundo_erro_nao_reseta_o_carimbo(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # O carimbo é DESDE QUANDO, não a última vez. Movê-lo a cada falha faria
        # a loja que falha de cinco em cinco minutos parecer eternamente recente
        # — que é exatamente o defeito de `last_sync_at` que esta coluna existe
        # para corrigir.
        store = create_connector_account(admin, two_tenants.a.id)
        admin.execute("select internal.finish_sync(%s, 'error', null)", (store.id,))
        admin.execute(
            "update public.connector_accounts set sync_error_since = now() - interval '3 hours'"
            " where id = %s",
            (store.id,),
        )

        admin.execute("select internal.finish_sync(%s, 'error', null)", (store.id,))

        aged = admin.execute(
            "select sync_error_since < now() - interval '2 hours'"
            "  from public.connector_accounts where id = %s",
            (store.id,),
        ).fetchone()[0]
        assert aged is True

    def test_um_passe_bem_sucedido_apaga_o_carimbo(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        store = create_connector_account(admin, two_tenants.a.id)
        admin.execute("select internal.finish_sync(%s, 'error', null)", (store.id,))

        admin.execute("select internal.finish_sync(%s, 'ok', null)", (store.id,))

        since = admin.execute(
            "select sync_error_since from public.connector_accounts where id = %s", (store.id,)
        ).fetchone()[0]
        assert since is None

    def test_uma_loja_em_erro_ha_horas_abre_alerta(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        store = create_connector_account(admin, two_tenants.a.id)
        admin.execute("select internal.finish_sync(%s, 'error', null)", (store.id,))
        admin.execute(
            "update public.connector_accounts set sync_error_since = now() - interval '4 hours'"
            " where id = %s",
            (store.id,),
        )

        assert sweep(admin) == 1

        (alert,) = alerts_of(admin, two_tenants.a.id)
        assert alert[0] == "connector_error"
        assert alert[1] == "warning"

    def test_uma_falha_recente_nao_abre_nada(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # Uma plataforma que oscila por dez minutos se resolve sozinha, e o
        # cinto de segurança do ADR-3 é feito de repetição. Alertar no primeiro
        # erro seria alertar sobre o próprio mecanismo funcionando.
        store = create_connector_account(admin, two_tenants.a.id)
        admin.execute("select internal.finish_sync(%s, 'error', null)", (store.id,))

        assert sweep(admin) == 0

    def test_o_alerta_nomeia_a_loja_e_a_plataforma_e_mais_nada(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        store = create_connector_account(admin, two_tenants.a.id)
        admin.execute("select internal.finish_sync(%s, 'error', null)", (store.id,))
        admin.execute(
            "update public.connector_accounts set sync_error_since = now() - interval '4 hours'"
            " where id = %s",
            (store.id,),
        )

        sweep(admin)

        payload = admin.execute(
            "select payload from public.alerts where type = 'connector_error'"
        ).fetchone()[0]
        assert payload["connector_account_id"] == str(store.id)
        assert payload["platform"] == "shopify"
        assert payload["error_for_seconds"] >= 4 * 3600

    def test_o_alerta_nao_se_repete_enquanto_estiver_aberto(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        store = create_connector_account(admin, two_tenants.a.id)
        admin.execute("select internal.finish_sync(%s, 'error', null)", (store.id,))
        admin.execute(
            "update public.connector_accounts set sync_error_since = now() - interval '4 hours'"
            " where id = %s",
            (store.id,),
        )

        assert sweep(admin) == 1
        assert sweep(admin) == 0


class TestOPasseDoRuntime:
    """A varredura tem de ser alcançável pela porta que a produção usa.

    *Guarda sem alvo mente*: uma função SQL que só um teste chama é um alerta que
    ninguém abre com passos a mais. O que segue exercita `queueing/health.py`, que
    é o que a tarefa periódica de `app.py` executa.
    """

    async def test_o_passe_abre_o_alerta_do_toque_preso(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        a_stuck_touch(admin, two_tenants.a.id, thread)

        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            opened = await health_pass(
                conn, touch_stuck_after=STUCK_AFTER, sync_error_after=SYNC_ERROR_AFTER
            )

        assert opened == 1
        assert alerts_of(admin, two_tenants.a.id)[0][0] == "touch_stuck"

    async def test_um_processo_saudavel_reporta_zero(self, dsn: str) -> None:
        # E é isto que mantém a lista do lojista legível: um número aqui sempre
        # significa que algo mudou, nunca "o mundo continua como estava".
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            assert (
                await health_pass(
                    conn, touch_stuck_after=STUCK_AFTER, sync_error_after=SYNC_ERROR_AFTER
                )
                == 0
            )


class TestQuemPodeVarrer:
    def test_o_worker_pode_chamar_a_varredura(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        # A varredura é uma tarefa periódica do processo, como o coalescer e a
        # varredura de silêncio: ela roda com o papel do worker.
        a_stuck_touch(admin, two_tenants.a.id, thread)

        with as_app_role(dsn, "worker_role", two_tenants.a.id) as worker:
            opened = worker.execute(
                "select internal.sweep_health_alerts(%s, %s)",
                (STUCK_AFTER, SYNC_ERROR_AFTER),
            ).fetchone()[0]

        assert opened == 1

    def test_o_sender_nao_pode(self, dsn: str, two_tenants: TwoTenants) -> None:
        # O molde de todo claim cross-tenant do repositório: EXECUTE revogado de
        # PUBLIC e concedido a um papel só. Quem varre não é quem envia.
        with as_app_role(dsn, "sender_role", two_tenants.a.id) as sender:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                sender.execute(
                    "select internal.sweep_health_alerts(%s, %s)",
                    (STUCK_AFTER, SYNC_ERROR_AFTER),
                )

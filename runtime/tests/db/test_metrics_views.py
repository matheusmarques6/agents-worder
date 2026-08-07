"""E3 · S11 — os números do marco como fato consultável.

O hub (E5) e o admin (E6) vão precisar de "quantos toques saíram, quantos
morreram e POR QUÊ, quanto voltou em dinheiro, e como está o número". Se cada
tela inventar a sua consulta, o mesmo número aparece diferente em dois lugares e
ninguém sabe qual está certo — e pior, o motivo do cancelamento, que é o
diagnóstico inteiro deste marco, vira nove `count(*)` que cada autor agrupa como
quiser (foi por isso que o vocabulário da escada entrou no `cancel_reason` no S2,
em vez dos quatro valores do dicionário).

Então o fato é uma VIEW, e a RLS é a mesma da tabela por baixo:
`security_invoker = true` faz a política do lojista valer sobre a view sem uma
única cláusula `WHERE tenant_id` escrita à mão — que é o que o `CLAUDE.md`
proíbe em voz alta ("ownership é imposto por RLS, nunca por WHERE escrito à
mão").

**Sem PII.** Estas views vão ser lidas por telas e, amanhã, raspadas para
métrica: nenhuma delas expõe telefone, nome ou conteúdo. `metrics_channel_health`
em particular nasce com GRANT por COLUNA, e não por tabela, porque
`channels_accounts` carrega o telefone e a referência do Vault ao lado dos
contadores.
"""

import uuid

import psycopg
import pytest

from tests.db.conftest import TwoTenants, as_app_role, as_authenticated_user
from tests.db.factories import Thread, create_channel_account, create_thread
from tests.db.factories_e3 import (
    create_funnel,
    create_funnel_conversion,
    create_scheduled_touch,
)

pytestmark = pytest.mark.db


@pytest.fixture
def thread(admin: psycopg.Connection, two_tenants: TwoTenants) -> Thread:
    return create_thread(admin, two_tenants.a.id)


def touch_rows(conn: psycopg.Connection, tenant_id: uuid.UUID) -> list[tuple]:
    return conn.execute(
        "select status, cancel_reason, touches from public.metrics_touches"
        "  where tenant_id = %s order by status, cancel_reason nulls first",
        (tenant_id,),
    ).fetchall()


class TestOsToquesPorDesfecho:
    def test_agendados_enviados_e_cancelados_contam_separados(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        funnel = create_funnel(admin, two_tenants.a.id)
        common = dict(conversation_id=thread.conversation_id)
        create_scheduled_touch(
            admin, two_tenants.a.id, funnel.id, thread.contact_id, touch_number=1, **common
        )
        create_scheduled_touch(
            admin,
            two_tenants.a.id,
            funnel.id,
            thread.contact_id,
            touch_number=2,
            status="sent",
            sent_ago_seconds=60,
            **common,
        )
        create_scheduled_touch(
            admin,
            two_tenants.a.id,
            funnel.id,
            thread.contact_id,
            touch_number=3,
            status="cancelled",
            cancel_reason="stale_newer_message",
            **common,
        )

        assert touch_rows(admin, two_tenants.a.id) == [
            ("cancelled", "stale_newer_message", 1),
            ("pending", None, 1),
            ("sent", None, 1),
        ]

    def test_os_cancelados_nao_sao_achatados_num_total(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        # É o motivo que diagnostica, não o total: "cancelou 40" não distingue
        # "o contato respondeu" (o funil funcionou) de "o número está pausado no
        # tier" (o produto está quebrado). O S2 pôs o vocabulário da escada no
        # `cancel_reason` exatamente para esta view existir.
        funnel = create_funnel(admin, two_tenants.a.id)
        for number, reason in enumerate(
            ("stale_newer_message", "stale_order_paid", "suppressed_block"), start=1
        ):
            create_scheduled_touch(
                admin,
                two_tenants.a.id,
                funnel.id,
                thread.contact_id,
                conversation_id=thread.conversation_id,
                touch_number=number,
                status="cancelled",
                cancel_reason=reason,
            )

        reasons = {row[1] for row in touch_rows(admin, two_tenants.a.id)}
        assert reasons == {"stale_newer_message", "stale_order_paid", "suppressed_block"}

    def test_a_ocasiao_do_funil_viaja_junto(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        # "Quantos toques o PIX pendente gerou" é a pergunta que o lojista faz, e
        # ela não é respondível pelo id do funil.
        funnel = create_funnel(admin, two_tenants.a.id, occasion="pix_pending")
        create_scheduled_touch(
            admin,
            two_tenants.a.id,
            funnel.id,
            thread.contact_id,
            conversation_id=thread.conversation_id,
        )

        occasion = admin.execute(
            "select occasion from public.metrics_touches where tenant_id = %s",
            (two_tenants.a.id,),
        ).fetchone()[0]
        assert occasion == "pix_pending"

    def test_a_view_nao_expoe_contato_nem_telefone(self, admin: psycopg.Connection) -> None:
        columns = {
            row[0]
            for row in admin.execute(
                "select column_name from information_schema.columns"
                "  where table_schema = 'public' and table_name = 'metrics_touches'"
            ).fetchall()
        }
        assert "contact_id" not in columns
        assert "phone_e164" not in columns


class TestOToquePresoTemOndeSerVisto:
    def test_o_backlog_lista_o_toque_parado_com_a_idade(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        # O alerta do S11 diz "há N presos, o pior desde X"; esta view é onde a
        # pessoa avisada vai achar QUAIS. Sem ela o alerta manda alguém escrever
        # SQL às três da manhã.
        funnel = create_funnel(admin, two_tenants.a.id)
        touch = create_scheduled_touch(
            admin,
            two_tenants.a.id,
            funnel.id,
            thread.contact_id,
            conversation_id=thread.conversation_id,
        )
        admin.execute(
            "update public.scheduled_touches"
            "   set status = 'enqueued', claimed_by = %s, claimed_at = now() - interval '2 hours'"
            " where id = %s",
            (uuid.uuid4(), touch),
        )

        (row,) = admin.execute(
            "select scheduled_touch_id, age_seconds from public.metrics_stuck_touches"
            "  where tenant_id = %s",
            (two_tenants.a.id,),
        ).fetchall()

        assert row[0] == touch
        assert row[1] >= 2 * 3600

    def test_um_toque_pendente_nao_esta_no_backlog(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        funnel = create_funnel(admin, two_tenants.a.id)
        create_scheduled_touch(
            admin,
            two_tenants.a.id,
            funnel.id,
            thread.contact_id,
            conversation_id=thread.conversation_id,
        )

        assert (
            admin.execute(
                "select count(*) from public.metrics_stuck_touches where tenant_id = %s",
                (two_tenants.a.id,),
            ).fetchone()[0]
            == 0
        )


class TestAsConversoes:
    def test_o_dinheiro_atribuido_soma_por_funil(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        funnel = create_funnel(admin, two_tenants.a.id)
        for amount in ("100.00", "50.50"):
            create_funnel_conversion(
                admin,
                two_tenants.a.id,
                funnel_id=funnel.id,
                contact_id=thread.contact_id,
                amount=amount,
            )

        conversions, amount, currency = admin.execute(
            "select conversions, amount, currency from public.metrics_conversions"
            "  where tenant_id = %s",
            (two_tenants.a.id,),
        ).fetchone()

        assert (conversions, str(amount), currency) == (2, "150.50", "BRL")

    def test_a_moeda_nao_e_somada_com_outra(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        # Somar BRL com USD produz um número que não é dinheiro nenhum. A moeda
        # é chave de agrupamento, não coluna decorativa.
        funnel = create_funnel(admin, two_tenants.a.id)
        create_funnel_conversion(
            admin, two_tenants.a.id, funnel_id=funnel.id, amount="100.00", currency="BRL"
        )
        create_funnel_conversion(
            admin, two_tenants.a.id, funnel_id=funnel.id, amount="10.00", currency="USD"
        )

        rows = admin.execute(
            "select currency, amount from public.metrics_conversions"
            "  where tenant_id = %s order by currency",
            (two_tenants.a.id,),
        ).fetchall()

        assert [(row[0], str(row[1])) for row in rows] == [("BRL", "100.00"), ("USD", "10.00")]


class TestASaudeDoNumero:
    def test_o_uso_do_tier_vem_como_fracao_pronta(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # A escada pausa em 0,8 (RF-035). Uma tela que dividisse sozinha seria a
        # terceira cópia da mesma conta; a view devolve a fração e a tela compara.
        account = create_channel_account(admin, two_tenants.a.id)
        admin.execute(
            "update public.channels_accounts set meta_tier = 1000, tier_usage_24h = 850"
            " where id = %s",
            (account.id,),
        )

        usage = admin.execute(
            "select tier_usage_fraction from public.metrics_channel_health"
            "  where channel_account_id = %s",
            (account.id,),
        ).fetchone()[0]

        assert float(usage) == pytest.approx(0.85)

    def test_um_numero_sem_tier_nao_inventa_fracao(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # Evolution não tem tier (D10): a fração é NULL, e não zero. Zero diria
        # "está folgado", que é uma afirmação sobre um limite que não existe.
        account = create_channel_account(admin, two_tenants.a.id, type="evolution")

        usage = admin.execute(
            "select tier_usage_fraction from public.metrics_channel_health"
            "  where channel_account_id = %s",
            (account.id,),
        ).fetchone()[0]

        assert usage is None

    def test_o_estagio_de_warmup_e_o_teto_do_dia_estao_la(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        account = create_channel_account(admin, two_tenants.a.id, type="evolution")

        stage, cap = admin.execute(
            "select warmup_stage, daily_cap from public.metrics_channel_health"
            "  where channel_account_id = %s",
            (account.id,),
        ).fetchone()

        assert (stage, cap) == (0, 300)

    def test_o_contador_do_dia_zera_quando_a_conta_e_de_ontem(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # O teto é diário, e um processo que atravessa a meia-noite não carrega a
        # conta de ontem — a mesma regra que `claim_outbox_batch` já aplica.
        account = create_channel_account(admin, two_tenants.a.id, type="evolution")
        admin.execute(
            "update public.channels_accounts"
            "   set sends_today = 42, sends_day = current_date - 1 where id = %s",
            (account.id,),
        )

        sends = admin.execute(
            "select sends_today from public.metrics_channel_health"
            "  where channel_account_id = %s",
            (account.id,),
        ).fetchone()[0]

        assert sends == 0

    def test_a_view_nao_expoe_telefone_nem_a_referencia_do_vault(
        self, admin: psycopg.Connection
    ) -> None:
        # `channels_accounts` carrega `phone_e164` e `vault_secret_id` ao lado dos
        # contadores. A view existe para ser lida por tela e raspada por métrica,
        # e nem uma nem outra têm o que fazer com esses dois.
        columns = {
            row[0]
            for row in admin.execute(
                "select column_name from information_schema.columns"
                "  where table_schema = 'public' and table_name = 'metrics_channel_health'"
            ).fetchall()
        }
        assert "phone_e164" not in columns
        assert "vault_secret_id" not in columns
        assert "external_account_id" not in columns


class TestARLSAtravessaAView:
    """A prova que justifica `security_invoker = true`.

    Uma view `security definer` (o default) roda com os privilégios de quem a
    criou, e a política do lojista simplesmente não se aplica — o mesmo defeito
    de escrever `WHERE tenant_id = ...` à mão, só que invisível no call site.
    """

    def test_o_lojista_ve_os_seus_toques_e_nao_os_do_vizinho(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        for tenant in (two_tenants.a, two_tenants.b):
            thread = create_thread(admin, tenant.id)
            funnel = create_funnel(admin, tenant.id)
            create_scheduled_touch(
                admin,
                tenant.id,
                funnel.id,
                thread.contact_id,
                conversation_id=thread.conversation_id,
            )

        with as_authenticated_user(dsn, two_tenants.a.user_id) as hub:
            tenants = {
                row[0]
                for row in hub.execute("select tenant_id from public.metrics_touches").fetchall()
            }

        assert tenants == {two_tenants.a.id}

    def test_o_lojista_ve_as_suas_conversoes_e_nao_as_do_vizinho(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        for tenant in (two_tenants.a, two_tenants.b):
            funnel = create_funnel(admin, tenant.id)
            create_funnel_conversion(admin, tenant.id, funnel_id=funnel.id)

        with as_authenticated_user(dsn, two_tenants.b.user_id) as hub:
            tenants = {
                row[0]
                for row in hub.execute(
                    "select tenant_id from public.metrics_conversions"
                ).fetchall()
            }

        assert tenants == {two_tenants.b.id}

    def test_o_lojista_ve_a_saude_do_seu_numero_e_nao_a_do_vizinho(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        for tenant in (two_tenants.a, two_tenants.b):
            create_channel_account(admin, tenant.id)

        with as_authenticated_user(dsn, two_tenants.a.user_id) as hub:
            tenants = {
                row[0]
                for row in hub.execute(
                    "select tenant_id from public.metrics_channel_health"
                ).fetchall()
            }

        assert tenants == {two_tenants.a.id}

    def test_o_worker_so_ve_o_tenant_do_seu_escopo(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # O runtime lê pelo `SET LOCAL app.tenant_id`, e a view não é uma porta
        # lateral para fora dele.
        for tenant in (two_tenants.a, two_tenants.b):
            thread = create_thread(admin, tenant.id)
            funnel = create_funnel(admin, tenant.id)
            create_scheduled_touch(
                admin,
                tenant.id,
                funnel.id,
                thread.contact_id,
                conversation_id=thread.conversation_id,
            )

        with as_app_role(dsn, "worker_role", two_tenants.a.id) as worker:
            tenants = {
                row[0]
                for row in worker.execute(
                    "select tenant_id from public.metrics_touches"
                ).fetchall()
            }

        assert tenants == {two_tenants.a.id}

    @pytest.mark.parametrize(
        "view",
        [
            "metrics_touches",
            "metrics_stuck_touches",
            "metrics_conversions",
            "metrics_channel_health",
        ],
    )
    def test_o_anonimo_nao_le_nada(self, dsn: str, view: str) -> None:
        # A Data API expõe o schema `public` inteiro: uma view nova sem REVOKE
        # explícito depende do default do banco para não vazar.
        #
        # `autocommit` e uma conexão por view de propósito: `SET ROLE` é
        # transacional, então um `rollback()` depois do erro devolveria a sessão
        # ao superusuário e a próxima iteração testaria a pessoa errada — que foi
        # exatamente como a primeira versão deste teste passou por engano.
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("set role anon")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f"select 1 from public.{view}")

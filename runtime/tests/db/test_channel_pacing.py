"""E3 · S7 — os fatos do número, e a aritmética de contá-los.

A REGRA não está aqui: ela mora em `queueing/antiban.py`, em Python, uma vez.
O que este arquivo cobra do banco são as três coisas que só o banco pode
garantir:

1. **o claim carrega o ritmo junto com a linha** — sem segunda consulta, porque
   entre a consulta e o envio o mundo muda (o comentário do tipo diz isso desde
   o E1);
2. **adiar não gasta tentativa** — um número esperando o jitter esgotaria as
   próprias retentativas se esperar contasse como falhar;
3. **o tier do Meta é contado e alerta na travessia dos 80%** — a pausa em si é
   da escada, que já lê estas colunas; o que faltava era o escritor.

E a assimetria que o produto inteiro depende: **reativa não conta e não espera.**
"""

import uuid

import psycopg
import pytest

from tests.db.conftest import TwoTenants
from tests.db.factories import Thread, create_channel_account, create_outbox_item, create_thread

pytestmark = pytest.mark.db

TIER_PAUSE_FRACTION = 0.8


def claim(conn: psycopg.Connection, token: uuid.UUID) -> list[tuple]:
    return conn.execute(
        "select * from internal.claim_outbox_batch(%s, %s)", (token, 50)
    ).fetchall()


def pacing_of(row: tuple) -> dict:
    """Os atributos que o S7 acrescentou, por nome."""
    return {
        "proactive": row[8],
        "channel_account_id": row[9],
        "risk_accepted": row[10],
        "warmup_stage": row[11],
        "daily_cap": row[12],
        "sends_today": row[13],
        "next_send_at": row[14],
    }


@pytest.fixture
def thread(admin: psycopg.Connection, two_tenants: TwoTenants) -> Thread:
    return create_thread(admin, two_tenants.a.id)


def account_of(admin: psycopg.Connection, thread: Thread) -> uuid.UUID:
    return thread.channel_account_id


class TestTheClaimCarriesTheRhythm:
    def test_a_funnel_touch_is_proactive_and_a_reply_is_not(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        # A pergunta "isto é disparo?" tem uma resposta só, e ela vem da coluna
        # que a outbox já carrega. Deixar o runtime deduzir por outro caminho
        # seria deixar dois lugares decidirem o mesmo fato.
        create_outbox_item(admin, two_tenants.a.id, thread, kind="funnel_touch")

        (row,) = claim(admin, uuid.uuid4())

        assert pacing_of(row)["proactive"] is True

    def test_a_reply_is_never_proactive(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        create_outbox_item(admin, two_tenants.a.id, thread, kind="reply")

        (row,) = claim(admin, uuid.uuid4())

        assert pacing_of(row)["proactive"] is False

    def test_the_numbers_own_facts_travel_with_the_row(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        account = account_of(admin, thread)
        admin.execute(
            """
            update public.channels_accounts
               set warmup_stage = 1, daily_cap = 42, risk_accepted_at = now()
             where id = %s
            """,
            (account,),
        )
        create_outbox_item(admin, two_tenants.a.id, thread, kind="funnel_touch")

        (row,) = claim(admin, uuid.uuid4())
        pacing = pacing_of(row)

        assert pacing["channel_account_id"] == account
        assert pacing["risk_accepted"] is True
        assert pacing["warmup_stage"] == 1
        assert pacing["daily_cap"] == 42

    def test_a_number_that_never_accepted_the_risk_says_so(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        # Falha fechada: um aceite que não se enxerga é um aceite que não houve.
        create_outbox_item(admin, two_tenants.a.id, thread, kind="funnel_touch")

        (row,) = claim(admin, uuid.uuid4())

        assert pacing_of(row)["risk_accepted"] is False

    def test_yesterdays_count_does_not_travel_into_today(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        # O teto é diário. Um processo que atravessa a meia-noite carregando a
        # conta de ontem entrega um dia inteiro a menos, em silêncio.
        admin.execute(
            "update public.channels_accounts set sends_today = 300,"
            " sends_day = current_date - 1 where id = %s",
            (account_of(admin, thread),),
        )
        create_outbox_item(admin, two_tenants.a.id, thread, kind="funnel_touch")

        (row,) = claim(admin, uuid.uuid4())

        assert pacing_of(row)["sends_today"] == 0


class TestDeferringIsNotFailing:
    def test_the_row_comes_back_pending_without_spending_an_attempt(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        # Um número em jitter esgotaria as próprias retentativas esperando a vez
        # se esperar contasse como tentar.
        outbox_id = create_outbox_item(admin, two_tenants.a.id, thread)
        token = uuid.uuid4()
        claim(admin, token)

        deferred = admin.execute(
            "select internal.defer_outbox_send(%s, %s, interval '75 seconds')",
            (outbox_id, token),
        ).fetchone()[0]

        status, attempts, future, error = admin.execute(
            "select status, attempt_count, next_attempt_at > now(), last_error"
            "  from internal.message_outbox where id = %s",
            (outbox_id,),
        ).fetchone()

        assert deferred is True
        assert status == "pending"
        assert attempts == 0, "o claim incrementou; adiar devolve"
        assert future is True
        assert error is None, "ninguém tentou, então não há erro para registrar"

    def test_only_the_owner_of_the_lease_may_defer(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        # A disciplina da lease, repetida: um sender atrasado que pudesse
        # devolver a linha que outro reivindicou faria a mensagem sair duas vezes.
        outbox_id = create_outbox_item(admin, two_tenants.a.id, thread)
        claim(admin, uuid.uuid4())

        deferred = admin.execute(
            "select internal.defer_outbox_send(%s, %s, interval '1 minute')",
            (outbox_id, uuid.uuid4()),
        ).fetchone()[0]

        assert deferred is False


class TestTheCounterAndTheTier:
    def test_a_proactive_send_is_counted_for_the_day(
        self, admin: psycopg.Connection, thread: Thread
    ) -> None:
        account = account_of(admin, thread)

        for _ in range(3):
            admin.execute(
                "select internal.record_channel_send(%s, true, null, %s)",
                (account, TIER_PAUSE_FRACTION),
            )

        sends_today, sends_day = admin.execute(
            "select sends_today, sends_day from public.channels_accounts where id = %s",
            (account,),
        ).fetchone()

        assert sends_today == 3
        assert sends_day is not None

    def test_a_reactive_send_is_not_counted_at_all(
        self, admin: psycopg.Connection, thread: Thread
    ) -> None:
        # O tier do Meta mede conversa INICIADA PELO NEGÓCIO. Contar a resposta
        # pausaria proativos por causa de um movimento que o Meta não cobra — e
        # o teto anti-ban existe contra disparo, não contra atendimento.
        account = account_of(admin, thread)
        admin.execute(
            "update public.channels_accounts set meta_tier = 10 where id = %s", (account,)
        )

        for _ in range(50):
            admin.execute(
                "select internal.record_channel_send(%s, false, null, %s)",
                (account, TIER_PAUSE_FRACTION),
            )

        sends_today, tier_usage = admin.execute(
            "select sends_today, tier_usage_24h from public.channels_accounts where id = %s",
            (account,),
        ).fetchone()

        assert (sends_today, tier_usage) == (0, 0)

    def test_the_jitter_of_the_next_send_is_written_on_the_number(
        self, admin: psycopg.Connection, thread: Thread
    ) -> None:
        account = account_of(admin, thread)

        admin.execute(
            "select internal.record_channel_send(%s, true, now() + interval '75 seconds', %s)",
            (account, TIER_PAUSE_FRACTION),
        )

        ahead = admin.execute(
            "select next_send_at > now() + interval '60 seconds'"
            "  from public.channels_accounts where id = %s",
            (account,),
        ).fetchone()[0]

        assert ahead is True

    def test_crossing_eighty_percent_of_the_tier_opens_exactly_one_alert(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
    ) -> None:
        # RF-035. A pausa é da escada; o alerta é daqui. E ele é UM: um alerta
        # por envio depois dos 80% é um alerta que ninguém lê.
        account = account_of(admin, thread)
        admin.execute(
            "update public.channels_accounts set meta_tier = 10 where id = %s", (account,)
        )

        for _ in range(10):
            admin.execute(
                "select internal.record_channel_send(%s, true, null, %s)",
                (account, TIER_PAUSE_FRACTION),
            )

        alerts = admin.execute(
            "select type, severity, payload->>'channel_account_id'"
            "  from public.alerts where tenant_id = %s",
            (two_tenants.a.id,),
        ).fetchall()

        assert alerts == [("meta_tier", "warning", str(account))]

    def test_the_alert_carries_no_pii(
        self, admin: psycopg.Connection, thread: Thread
    ) -> None:
        # `CLAUDE.md`: PII nunca sai do Postgres para a telemetria, e um alerta é
        # lido fora. Nem telefone, nem conteúdo.
        account, phone = admin.execute(
            "select id, phone_e164 from public.channels_accounts where id = %s",
            (account_of(admin, thread),),
        ).fetchone()
        admin.execute(
            "update public.channels_accounts set meta_tier = 1 where id = %s", (account,)
        )

        admin.execute(
            "select internal.record_channel_send(%s, true, null, %s)",
            (account, TIER_PAUSE_FRACTION),
        )

        payload, title = admin.execute(
            "select payload::text, title from public.alerts where type = 'meta_tier'"
        ).fetchone()

        assert phone not in payload
        assert phone not in title

    def test_the_tier_window_rolls_instead_of_growing_for_ever(
        self, admin: psycopg.Connection, thread: Thread
    ) -> None:
        # Sem janela rolante, `tier_usage_24h` só cresce e um número saudável
        # fica pausado para sempre depois do primeiro dia movimentado.
        account = account_of(admin, thread)
        admin.execute(
            "update public.channels_accounts set meta_tier = 1000 where id = %s", (account,)
        )

        admin.execute(
            "select internal.record_channel_send(%s, true, null, %s)",
            (account, TIER_PAUSE_FRACTION),
        )
        admin.execute(
            "update public.channels_accounts"
            "   set tier_window_started_at = now() - interval '25 hours' where id = %s",
            (account,),
        )
        admin.execute(
            "select internal.record_channel_send(%s, true, null, %s)",
            (account, TIER_PAUSE_FRACTION),
        )

        usage = admin.execute(
            "select tier_usage_24h from public.channels_accounts where id = %s", (account,)
        ).fetchone()[0]

        assert usage == 1


class TestTheRiskAcceptance:
    def test_accepting_writes_the_date_and_the_trail_together(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # Um aceite sem trilha é a nossa palavra contra a do lojista no dia do
        # banimento.
        account = create_channel_account(admin, two_tenants.a.id, type="evolution")

        accepted = admin.execute(
            "select internal.accept_channel_risk(%s)", (account.id,)
        ).fetchone()[0]

        risk_accepted_at = admin.execute(
            "select risk_accepted_at from public.channels_accounts where id = %s", (account.id,)
        ).fetchone()[0]
        trail = admin.execute(
            "select action, target_type, target_id, actor_type from public.audit_log"
            " where tenant_id = %s",
            (two_tenants.a.id,),
        ).fetchall()

        assert accepted is True
        assert risk_accepted_at is not None
        assert trail == [("channel.risk_accepted", "channels_accounts", account.id, "system")]

    def test_accepting_twice_does_not_move_the_date(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # A data É a prova. Movê-la apagaria desde quando o aceite vale.
        account = create_channel_account(admin, two_tenants.a.id, type="evolution")
        admin.execute("select internal.accept_channel_risk(%s)", (account.id,))
        first = admin.execute(
            "select risk_accepted_at from public.channels_accounts where id = %s", (account.id,)
        ).fetchone()[0]

        again = admin.execute(
            "select internal.accept_channel_risk(%s)", (account.id,)
        ).fetchone()[0]

        second = admin.execute(
            "select risk_accepted_at from public.channels_accounts where id = %s", (account.id,)
        ).fetchone()[0]

        assert again is False
        assert second == first

    def test_the_runtime_roles_may_not_accept_the_risk_for_anybody(
        self, admin: psycopg.Connection
    ) -> None:
        # A assinatura existe para ser humana. Concedê-la ao processo automático
        # "por precaução" é o processo assinando pelo lojista.
        holders = admin.execute(
            """
            -- `grantee` é `sql_identifier`, e um array desse tipo volta como
            -- texto cru para o driver. O cast é o que faz a asserção comparar
            -- uma lista com uma lista.
            select coalesce(array_agg(distinct grantee::text order by grantee::text),
                            array[]::text[])
              from information_schema.routine_privileges
             where routine_name = 'accept_channel_risk'
               and grantee in ('worker_role', 'sender_role', 'PUBLIC')
            """
        ).fetchone()[0]

        assert holders == []


class TestTheClaimStaysTheSenderS:
    def test_the_worker_still_cannot_drain_the_outbox(
        self, admin: psycopg.Connection
    ) -> None:
        # A recriação do tipo e da função no S7 poderia ter devolvido o EXECUTE
        # a PUBLIC sem ninguém notar: `drop` leva os grants junto, e um `grant`
        # esquecido vira "todo mundo pode".
        holders = admin.execute(
            """
            -- `grantee` é `sql_identifier`, e um array desse tipo volta como
            -- texto cru para o driver. O cast é o que faz a asserção comparar
            -- uma lista com uma lista.
            select coalesce(array_agg(distinct grantee::text order by grantee::text),
                            array[]::text[])
              from information_schema.routine_privileges
             where routine_name = 'claim_outbox_batch'
            """
        ).fetchone()[0]

        assert "PUBLIC" not in holders
        assert "worker_role" not in holders
        assert "sender_role" in holders

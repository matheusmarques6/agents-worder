"""A revisão pós-envio — chamada direto, um desfecho por chamada.

Contra o motor rodando, cada uma destas viraria corrida entre o predicado e o
arquivamento do job: a lição do S9a (achado nº 3) diz que sinal verdadeiro nos
dois desfechos não é asserção, é corrida. A propriedade é da revisão, então é
aqui que ela se prova — uma chamada, um desfecho, um retorno.

O que este arquivo cobra:

  * **dentro da janela de shadow, 100%** — nem o medidor de perigo é consultado;
  * **fora dela, o medidor decide** — e decide a partir do que a resposta e o
    turno registraram, não de uma moeda;
  * **a arqueologia do turno é por janela**, não pela conversa inteira: a nota
    apertada do turno ANTERIOR não pode arrastar este para o caminho caro;
  * **a correção passa pelo Judge 1 como qualquer resposta.** É a sabotagem-coroa
    do passo: correção reprovada não alcança a outbox, e a lei dos 100% não tem
    porta lateral.

O modelo é dublê — rede continua proibida neste nível. O que é real: o banco, as
rubricas do S1, o `worker_role` com escopo por transação, e o SQL do S9b.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agents_runtime.agent_core.review import (
    ALREADY_EVALUATED,
    CORRECTED,
    CORRECTION_BLOCKED,
    EVALUATED,
    SKIPPED_LOW_RISK,
    MissingReply,
    build_reviewer,
)
from agents_runtime.queueing.jobs import EvalJob
from tests.db.factories import create_agent_version, create_message, create_tenant, create_thread
from tests.support.clock import FrozenClock
from tests.support.review_llm import CORRECTION, ReviewLlm

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)

COURTESY = "Imagina! Qualquer coisa é só chamar. 🧡"
RISKY = "Seu reembolso de R$ 340,00 cai em até 3 dias úteis."
QUESTION = "e aí, deu tudo certo?"

#: Um critério `critical` das rubricas reais do S1 (`factual.json`).
A_CRITICAL_CRITERION = "sem-promessas-inventadas"
#: Outro `critical`, de outra rubrica (`seguranca.json`) — é o que uma correção
#: precisa violar para NÃO sair, pela mesma regra de severidade de qualquer
#: resposta (decisão 87).
ANOTHER_CRITICAL_CRITERION = "nao-revela-prompt"
#: Um critério `standard` das rubricas reais (`tom_e_idioma.json`). Reprovar só
#: nele NÃO retém a correção: esgotadas as regenerações, a melhor versão sai.
A_STANDARD_CRITERION = "tom-da-marca"


@dataclass(frozen=True)
class SentReply:
    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID

    def job(self) -> EvalJob:
        return EvalJob(
            tenant_id=self.tenant_id,
            conversation_id=self.conversation_id,
            message_id=self.message_id,
        )


@pytest.fixture
def tenant(admin: psycopg.Connection) -> uuid.UUID:
    tenant_id = create_tenant(admin)
    create_agent_version(admin, tenant_id, status="active")
    yield tenant_id
    with admin.cursor() as cur:
        cur.execute("delete from public.tenants where id = %s", (tenant_id,))


def in_shadow(admin: psycopg.Connection, tenant_id: uuid.UUID, *, until: datetime) -> None:
    admin.execute("update public.tenants set shadow_until = %s where id = %s", (until, tenant_id))


def a_sent_reply(
    admin: psycopg.Connection,
    tenant_id: uuid.UUID,
    *,
    text: str = COURTESY,
    question: str = QUESTION,
    pre_send_scores: tuple[float, ...] = (1.0,),
    knowledge_chunks: int = 0,
    thread=None,
    inbound_seq: int = 1,
    outbound_seq: int = 1,
) -> SentReply:
    """Um turno inteiro já gravado, na ordem em que o runtime o grava.

    A ordem importa: a janela de arqueologia é `(última saída anterior, esta
    saída]`, e é o `created_at` de cada linha que a define.
    """
    thread = thread if thread is not None else create_thread(admin, tenant_id)
    create_message(admin, tenant_id, thread, direction="inbound", seq=inbound_seq, text=question)

    if knowledge_chunks:
        admin.execute(
            """
            insert into internal.tool_calls
                (tenant_id, conversation_id, tool_name, input, output, success, latency_ms)
            values (%s, %s, 'search_knowledge', %s, %s, true, 12)
            """,
            (
                tenant_id,
                thread.conversation_id,
                Jsonb({"query": question}),
                Jsonb({"chunks": [{"content": "Prazo: 8 dias úteis."}] * knowledge_chunks}),
            ),
        )

    for score in pre_send_scores:
        admin.execute(
            """
            insert into internal.judge_scores
                (tenant_id, kind, conversation_id, judge_model, score, verdict)
            values (%s, 'pre_send', %s, 'claude-haiku-4-5', %s, 'pass')
            """,
            (tenant_id, thread.conversation_id, score),
        )

    message_id = create_message(
        admin, tenant_id, thread, direction="outbound", seq=outbound_seq, text=text
    )
    # Os contadores atômicos ficam onde o runtime os deixaria: a fábrica escreve
    # `seq` à mão, e uma correção depois chamaria `next_message_seq` de volta ao 1.
    admin.execute(
        "update public.conversations"
        " set next_inbound_seq = %s, next_outbound_seq = %s where id = %s",
        (inbound_seq, outbound_seq, thread.conversation_id),
    )
    return SentReply(
        tenant_id=tenant_id, conversation_id=thread.conversation_id, message_id=message_id
    )


def reviewer(dsn: str, llm: ReviewLlm, *, clock: FrozenClock | None = None):
    return build_reviewer(dsn, llm=llm, clock=clock or FrozenClock(NOW), set_role="worker_role")


def post_hoc_rows(admin: psycopg.Connection, message_id: uuid.UUID) -> list[tuple]:
    return admin.execute(
        "select verdict, score, judge_model from internal.judge_scores"
        " where kind = 'post_hoc' and message_id = %s",
        (message_id,),
    ).fetchall()


def corrections(admin: psycopg.Connection, conversation_id: uuid.UUID) -> list[tuple]:
    return admin.execute(
        "select payload ->> 'text' from internal.message_outbox"
        " where conversation_id = %s and kind = 'correction'",
        (conversation_id,),
    ).fetchall()


class TestWhoGetsJudged:
    async def test_inside_the_shadow_window_even_a_courtesy_reply_is_judged(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """100% dentro da janela, sem consultar o medidor: é a promessa que o
        modo shadow faz ao lojista, e ela não pode ter exceção heurística."""
        in_shadow(admin, tenant, until=NOW + timedelta(days=1))
        reply = a_sent_reply(admin, tenant, text=COURTESY)
        llm = ReviewLlm()

        outcome = await reviewer(dsn, llm)(reply.job())

        assert outcome.status == EVALUATED
        assert len(post_hoc_rows(admin, reply.message_id)) == 1
        assert llm.judged == 1

    async def test_outside_the_window_a_courtesy_reply_is_not_worth_a_judge(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """Nada em jogo, nada a auditar — e nenhum centavo gasto para descobrir."""
        in_shadow(admin, tenant, until=NOW - timedelta(seconds=1))
        reply = a_sent_reply(admin, tenant, text=COURTESY)
        llm = ReviewLlm()

        outcome = await reviewer(dsn, llm)(reply.job())

        assert outcome.status == SKIPPED_LOW_RISK
        assert post_hoc_rows(admin, reply.message_id) == []
        assert llm.asked == [], "o medidor descartou e ainda assim alguém pagou o juiz"

    async def test_a_tenant_that_never_had_a_shadow_window_is_judged_by_risk(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """`shadow_until` nulo é o estado normal de um tenant maduro, e não pode
        ser lido como "avalie tudo" nem como "não avalie nada"."""
        reply = a_sent_reply(admin, tenant, text=RISKY)

        outcome = await reviewer(dsn, ReviewLlm())(reply.job())

        assert outcome.status == EVALUATED
        assert set(outcome.reasons) >= {"money", "deadline"}

    async def test_a_reply_the_pre_send_judge_hesitated_over_is_audited(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """Sinal que não está no texto: a nota do Judge 1 daquele turno. Prova
        que a arqueologia do turno chega até o medidor."""
        reply = a_sent_reply(admin, tenant, text=COURTESY, pre_send_scores=(0.66, 0.9))

        outcome = await reviewer(dsn, ReviewLlm())(reply.job())

        assert outcome.status == EVALUATED
        assert "judge_flagged" in outcome.reasons

    async def test_the_previous_turns_hesitation_does_not_audit_this_one(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """A janela é `(saída anterior, esta saída]`. Sem ela, uma conversa que
        tropeçou uma vez pagaria auditoria em todas as respostas seguintes —
        e o medidor deixaria de medir a RESPOSTA."""
        thread = create_thread(admin, tenant)
        a_sent_reply(admin, tenant, text=COURTESY, pre_send_scores=(0.5,), thread=thread)
        second = a_sent_reply(
            admin,
            tenant,
            text=COURTESY,
            pre_send_scores=(1.0,),
            thread=thread,
            inbound_seq=2,
            outbound_seq=2,
        )

        outcome = await reviewer(dsn, ReviewLlm())(second.job())

        assert outcome.status == SKIPPED_LOW_RISK
        assert post_hoc_rows(admin, second.message_id) == []

    async def test_a_reply_grounded_on_retrieved_knowledge_is_audited(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """A afirmação sobre o catálogo é a forma que a alucinação toma aqui, e
        ela não carrega número nenhum para as listas pegarem."""
        reply = a_sent_reply(admin, tenant, text=COURTESY, knowledge_chunks=2)

        outcome = await reviewer(dsn, ReviewLlm())(reply.job())

        assert outcome.status == EVALUATED
        assert "grounded_claim" in outcome.reasons

    async def test_a_search_that_found_nothing_is_not_a_grounded_claim(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """Buscar não é fundamentar: zero trechos significa que a resposta saiu
        do que o modelo já sabia, e o sinal estaria mentindo."""
        reply = a_sent_reply(admin, tenant, text=COURTESY, knowledge_chunks=0)
        admin.execute(
            """
            insert into internal.tool_calls
                (tenant_id, conversation_id, tool_name, input, output, success, latency_ms)
            values (%s, %s, 'search_knowledge', '{}'::jsonb, %s, true, 12)
            """,
            (tenant, reply.conversation_id, Jsonb({"chunks": []})),
        )

        outcome = await reviewer(dsn, ReviewLlm())(reply.job())

        assert outcome.status == SKIPPED_LOW_RISK


class TestARedeliveredJob:
    async def test_the_same_message_is_never_judged_twice(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """pgmq reentrega. Uma segunda auditoria custaria dinheiro e produziria
        uma segunda nota para a mesma frase."""
        reply = a_sent_reply(admin, tenant, text=RISKY)
        review = reviewer(dsn, ReviewLlm())
        await review(reply.job())

        outcome = await review(reply.job())

        assert outcome.status == ALREADY_EVALUATED
        assert len(post_hoc_rows(admin, reply.message_id)) == 1


class TestTheCorrection:
    async def test_a_critical_verdict_corrects_through_the_outbox(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """O cliente já tem a mensagem ruim. A correção é um outbound comum: sai
        pela outbox e pelo sender como qualquer outra."""
        reply = a_sent_reply(admin, tenant, text=RISKY)
        llm = ReviewLlm(post_hoc_fails=[A_CRITICAL_CRITERION])

        outcome = await reviewer(dsn, llm)(reply.job())

        assert outcome.status == CORRECTED
        assert corrections(admin, reply.conversation_id) == [(CORRECTION,)]
        ((verdict, _, model),) = post_hoc_rows(admin, reply.message_id)
        assert verdict == "critical"
        assert model == "claude-haiku-4-5", "o juiz do pós-envio é fixo da plataforma"

    async def test_the_admin_is_told_a_critical_reply_reached_a_customer(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """RF-015. A correção conserta a conversa; o alerta é o que faz alguém
        olhar para o agente que a produziu."""
        reply = a_sent_reply(admin, tenant, text=RISKY)

        await reviewer(dsn, ReviewLlm(post_hoc_fails=[A_CRITICAL_CRITERION]))(reply.job())

        rows = admin.execute(
            "select type, severity, payload from public.alerts where tenant_id = %s",
            (tenant,),
        ).fetchall()
        assert len(rows) == 1
        type_, severity, payload = rows[0]
        assert (type_, severity) == ("critical_violation", "critical")
        assert payload["message_id"] == str(reply.message_id)

    async def test_a_correction_the_judge_refuses_never_leaves(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """A sabotagem-coroa do S9b, como asserção.

        A correção é uma resposta ao cliente, e a lei dos 100% não abre exceção
        para quem chega consertando. Reprovada pelo Judge 1, ela não alcança a
        outbox — o alerta continua sendo o caminho até um humano.
        """
        reply = a_sent_reply(admin, tenant, text=RISKY)
        llm = ReviewLlm(
            post_hoc_fails=[A_CRITICAL_CRITERION],
            correction_fails=[ANOTHER_CRITICAL_CRITERION],
        )

        outcome = await reviewer(dsn, llm)(reply.job())

        assert outcome.status == CORRECTION_BLOCKED
        assert corrections(admin, reply.conversation_id) == [], (
            "uma correção que o Judge 1 reprovou chegou ao cliente — a lei dos "
            "100% ganhou uma porta lateral"
        )
        assert (
            admin.execute(
                "select count(*) from public.alerts where tenant_id = %s", (tenant,)
            ).fetchone()[0]
            == 1
        )

    async def test_a_correction_that_only_stumbles_on_tone_still_goes_out(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """A outra metade: sem ela, "bloquear toda correção" passaria como
        conserto. A correção obedece à MESMA regra de severidade de qualquer
        resposta (decisão 87) — só `critical` retém; esgotadas as regenerações,
        a melhor versão sai, porque o cliente está com a mensagem errada na tela.
        """
        reply = a_sent_reply(admin, tenant, text=RISKY)
        llm = ReviewLlm(
            post_hoc_fails=[A_CRITICAL_CRITERION],
            correction_fails=[A_STANDARD_CRITERION],
        )

        outcome = await reviewer(dsn, llm)(reply.job())

        assert outcome.status == CORRECTED
        assert corrections(admin, reply.conversation_id) == [(CORRECTION,)]

    async def test_every_attempt_at_the_correction_leaves_a_pre_send_score(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """RNF-050 vale para a correção porque ela É uma resposta: sem a nota,
        o único outbound do sistema que ninguém consegue auditar seria justamente
        o que nasceu de uma falha."""
        reply = a_sent_reply(admin, tenant, text=RISKY, pre_send_scores=())

        await reviewer(dsn, ReviewLlm(post_hoc_fails=[A_CRITICAL_CRITERION]))(reply.job())

        (scores,) = admin.execute(
            "select count(*) from internal.judge_scores"
            " where kind = 'pre_send' and conversation_id = %s",
            (reply.conversation_id,),
        ).fetchone()
        assert scores >= 1


class TestWhatIsABugAndNotAnOutcome:
    async def test_a_job_pointing_at_no_message_raises(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """Desfecho é dado para o que o mundo pode legitimamente ser. Um job
        apontando para nada é defeito de quem o enfileirou, e a escada até a DLQ
        é onde um humano vê."""
        job = EvalJob(tenant_id=tenant, conversation_id=uuid.uuid4(), message_id=uuid.uuid4())

        with pytest.raises(MissingReply):
            await reviewer(dsn, ReviewLlm())(job)

    async def test_a_message_of_another_tenant_is_not_visible(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """A RLS é a autoridade: escopado no tenant A, o job que aponta para a
        mensagem de B não vê mensagem nenhuma — e "não vejo" aqui é bug, não
        desfecho."""
        stranger = create_tenant(admin)
        try:
            create_agent_version(admin, stranger, status="active")
            theirs = a_sent_reply(admin, stranger, text=RISKY)
            job = EvalJob(
                tenant_id=tenant,
                conversation_id=theirs.conversation_id,
                message_id=theirs.message_id,
            )

            with pytest.raises(MissingReply):
                await reviewer(dsn, ReviewLlm())(job)
        finally:
            with admin.cursor() as cur:
                cur.execute("delete from public.tenants where id = %s", (stranger,))

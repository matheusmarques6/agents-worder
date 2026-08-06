"""A revisão pós-envio — o outro lado do turno.

O responder (S9a) decide o que dizer; isto decide o que a plataforma aprende
depois de dito, e o que fazer quando o que foi dito estava errado. Mora ao lado
do responder pela mesma razão que ele mora aqui: é orquestração de um turno —
lê, decide, chama modelo, grava rastro. Os juízes que ela usa continuam em
`judges/`.

**Nunca retém envio.** Quando este código roda, o cliente já leu a mensagem. O
medidor de perigo escolhe o que auditar; a segurança continua sendo 100% do
Judge 1 pré-envio, e um falso negativo aqui custa medição, jamais proteção.

**Desfechos são DADO** (decisão 74): o consumidor arquiva em todos. Exceção fica
reservada para o que É bug — um job apontando para mensagem que não existe.

**A nota do pós-envio é gravada por último**, e isso é ordem, não estilo: ela é
a marca de "revisado". Gravá-la antes da correção faria uma morte no meio virar
silêncio — a reentrega veria `already_evaluated` e o cliente nunca receberia o
conserto. Na ordem certa, a morte no meio custa um segundo julgamento (dinheiro)
e nunca uma correção perdida (o cliente).
"""

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

from agents_runtime.agent_core import openrouter
from agents_runtime.agent_core.llm import ChatRequest, LlmPort, Message
from agents_runtime.agent_core.metering import MeteredLlm
from agents_runtime.agent_core.responder import default_rubrics_directory
from agents_runtime.agent_core.think_gate import PendingMessage, should_think
from agents_runtime.clock import Clock, SystemClock
from agents_runtime.evals.pack import load_rubrics
from agents_runtime.judges.post_hoc import JUDGE_PURPOSE, PostHocJudge
from agents_runtime.judges.pre_send import (
    CRITICAL,
    JUDGE_MODEL,
    JudgeContext,
    PreSendJudge,
    guarded_reply,
)
from agents_runtime.judges.risk_gate import SentReply, assess_risk
from agents_runtime.queueing.jobs import EvalJob
from agents_runtime.repository import agent as agent_repo
from agents_runtime.repository import alerts as alerts_repo
from agents_runtime.repository import judge_scores as scores_repo
from agents_runtime.repository import llm_calls as llm_repo
from agents_runtime.repository import review as review_repo
from agents_runtime.repository.scope import scope_to_tenant

# --- os desfechos, como vocabulário fechado ----------------------------------
#: A resposta foi julgada e o veredito não pede conserto.
EVALUATED = "evaluated"
#: Fora da janela de shadow e sem nenhum sinal de risco: nada em jogo.
SKIPPED_LOW_RISK = "skipped_low_risk"
#: Reentrega de um job cuja revisão já terminou.
ALREADY_EVALUATED = "already_evaluated"
#: Veredito `critical` e a correção está na outbox.
CORRECTED = "corrected"
#: Veredito `critical`, mas o Judge 1 reprovou a correção — ela NÃO sai.
CORRECTION_BLOCKED = "correction_blocked"
#: A conta do canal sumiu: não dá para corrigir, e fingir que deu seria pior.
NO_CHANNEL = "no_channel"
#: O payload do job não tem a forma do contrato. Não melhora numa segunda leitura.
INVALID_PAYLOAD = "invalid_payload"

#: `internal.correction_outcome`, do lado de cá.
_SENT, _ALREADY_SENT = "sent", "already_sent"

#: O que o pós-envio pede ao modelo do TENANT (nunca ao da plataforma: escrever
#: ao cliente é papel do agente da loja, julgar é papel do portão).
CORRECTION_PURPOSE = "agent_reply"

#: Quando não há mensagem do contato na janela, o think-gate não tem o que ler.
DEFAULT_THINK_REASON = "simple_input"


class MissingReply(LookupError):
    """O job aponta para uma mensagem enviada que esta conexão não vê.

    Não é desfecho: ou quem enfileirou errou, ou o escopo está errado. `LookupError`
    classifica como permanente (`queueing.failures`), então a escada leva direto à
    DLQ, onde um humano olha — em vez de cinco tentativas contra o inevitável.
    """


class NoActiveVersion(RuntimeError):
    """Sem versão ativa não há modelo para redigir a correção. Concluir em
    silêncio deixaria o cliente com a mensagem errada e ninguém sabendo."""


#: A costura que o consumidor de `q_evals` conhece — o irmão de `Responder`.
Reviewer = Callable[["EvalJob"], Awaitable["ReviewOutcome"]]


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    """O desfecho e o porquê. O motivo é dado porque "por que esta conversa foi
    auditada?" é pergunta que o hub e o S10 fazem de rotina."""

    status: str
    reasons: tuple[str, ...] = ()
    verdict: str | None = None


def build_reviewer(
    dsn: str,
    *,
    llm: LlmPort,
    clock: Clock | None = None,
    rubrics_directory: Path | None = None,
    set_role: str | None = None,
):
    """A revisão pós-envio como o consumidor de `q_evals` a conhece:
    `review(job) -> ReviewOutcome`."""
    clock = clock or SystemClock()
    rubrics = load_rubrics(rubrics_directory or default_rubrics_directory())

    async def review(job: EvalJob) -> ReviewOutcome:
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            if set_role:
                await conn.execute("set role " + set_role)

            # --- leitura: uma transação curta, fechada antes de qualquer rede
            async with conn.transaction():
                await scope_to_tenant(conn, job.tenant_id)
                already = await review_repo.post_hoc_score_exists(conn, message_id=job.message_id)
                reply = await review_repo.load_sent_reply(conn, message_id=job.message_id)
                if reply is None:
                    raise MissingReply(
                        f"evaluation job points at no outbound message: {job.message_id}"
                    )
                window = await review_repo.load_turn_window(
                    conn,
                    conversation_id=reply.conversation_id,
                    since=reply.since,
                    until=reply.sent_at,
                )
                settings = await agent_repo.load_tenant_policy(conn, tenant_id=job.tenant_id)
                version = await agent_repo.load_active_version(conn, tenant_id=job.tenant_id)

            if already:
                return ReviewOutcome(status=ALREADY_EVALUATED)

            reasons = _why_audit(reply, window, settings.shadow_until, clock)
            if reasons is None:
                return ReviewOutcome(status=SKIPPED_LOW_RISK)

            metered = partial(_metered, conn, job, llm, clock)
            context = JudgeContext(
                conversation=tuple(f"{message.author}: {message.text}" for message in window),
                language=settings.primary_language,
                never_say_ai=settings.never_say_ai,
            )

            judgement = await PostHocJudge(metered(JUDGE_PURPOSE), rubrics)(reply.text, context)

            status = EVALUATED
            if judgement.outcome == CRITICAL:
                if version is None:
                    raise NoActiveVersion(
                        f"tenant {job.tenant_id} has no active agent version to correct with"
                    )
                status = await _repair(
                    conn,
                    job,
                    reply,
                    judgement,
                    context=context,
                    rubrics=rubrics,
                    metered=metered,
                    model=version.config.model,
                )

            async with conn.transaction():
                await scope_to_tenant(conn, job.tenant_id)
                await review_repo.record_post_hoc_score(
                    conn,
                    tenant_id=job.tenant_id,
                    conversation_id=reply.conversation_id,
                    message_id=job.message_id,
                    judge_model=JUDGE_MODEL,
                    score=judgement.score,
                    verdict=judgement.outcome,
                    rationale=judgement.rationale,
                )

            return ReviewOutcome(status=status, reasons=reasons, verdict=judgement.outcome)

    return review


def agent_reviewer(dsn: str):
    """A fábrica que `AGENTS_REVIEWER` aponta — a auditoria real em produção."""
    return build_reviewer(
        dsn,
        llm=openrouter.from_env(),
        set_role=os.environ.get("AGENTS_WORKER_SET_ROLE"),
    )


def _why_audit(
    reply: review_repo.SentReplyRow,
    window: tuple[review_repo.WindowMessage, ...],
    shadow_until,
    clock: Clock,
) -> tuple[str, ...] | None:
    """Os motivos para auditar, ou None quando não há nenhum.

    Dentro da janela de shadow são 100%, e o medidor nem é consultado: a
    promessa que o modo shadow faz ao lojista não admite exceção heurística.
    """
    if shadow_until is not None and clock.now() < shadow_until:
        return ("shadow",)

    decision = assess_risk(
        SentReply(
            text=reply.text,
            used_knowledge=reply.used_knowledge,
            judge_score=reply.judge_score,
            judge_attempts=max(reply.judge_attempts, 1),
            think_reason=_think_reason(window),
        )
    )
    return decision.reasons if decision.evaluate else None


def _think_reason(window: tuple[review_repo.WindowMessage, ...]) -> str:
    """A leitura que o think-gate fez do contato, redescoberta.

    O gate é puro, então as mesmas mensagens dão sempre a mesma resposta — e
    redescobrir é mais honesto que gravar uma cópia que pode divergir. Janela
    sem contato acontece (uma correção, um toque de funil) e não é erro aqui.
    """
    pending = tuple(PendingMessage(author=message.author, text=message.text) for message in window)
    if not pending:
        return DEFAULT_THINK_REASON
    return should_think(pending).reason


async def _repair(
    conn: psycopg.AsyncConnection,
    job: EvalJob,
    reply: review_repo.SentReplyRow,
    judgement,
    *,
    context: JudgeContext,
    rubrics,
    metered,
    model: str,
) -> str:
    """O que se faz com uma violação que já chegou ao cliente.

    O alerta vem PRIMEIRO, como no não-envio do S8: uma morte no meio deixa
    alerta duplicado (benigno e visível) em vez de silêncio.
    """
    async with conn.transaction():
        await scope_to_tenant(conn, job.tenant_id)
        await alerts_repo.open_alert(
            conn,
            tenant_id=job.tenant_id,
            type=alerts_repo.CRITICAL_VIOLATION,
            severity="critical",
            title="Judge 2 reprovou uma resposta que já foi enviada",
            payload={
                "conversation_id": str(reply.conversation_id),
                "message_id": str(job.message_id),
                "failed_criteria": list(judgement.failed_criteria),
            },
        )

    correction = await _correction(
        job,
        reply,
        judgement,
        context=context,
        rubrics=rubrics,
        metered=metered,
        model=model,
        conn=conn,
    )
    if correction is None:
        return CORRECTION_BLOCKED

    async with conn.transaction():
        await scope_to_tenant(conn, job.tenant_id)
        outcome = await review_repo.enqueue_correction(
            conn, message_id=job.message_id, content={"text": correction}
        )
    return CORRECTED if outcome in (_SENT, _ALREADY_SENT) else NO_CHANNEL


async def _correction(
    job: EvalJob,
    reply: review_repo.SentReplyRow,
    judgement,
    *,
    context: JudgeContext,
    rubrics,
    metered,
    model: str,
    conn: psycopg.AsyncConnection,
) -> str | None:
    """O texto do conserto, ou None quando o Judge 1 não o liberou.

    A correção é uma resposta ao cliente como qualquer outra, então passa pelo
    mesmo portão: a lei dos 100% não tem porta lateral. Reprovada, o cliente
    fica com a mensagem errada e o alerta é o caminho até um humano — que é o
    desfecho ruim, mas honesto; mandar uma segunda mensagem errada seria pior.
    """
    chat = metered(CORRECTION_PURPOSE)

    async def generate(attempt: int, feedback: tuple[str, ...]) -> str:
        messages = [
            Message(role="system", content=_correction_brief(reply.text, judgement)),
            *[Message(role="user", content=line) for line in context.conversation],
        ]
        if feedback:
            messages.append(
                Message(
                    role="system",
                    content=(
                        "A correção anterior foi reprovada nos critérios: "
                        f"{', '.join(feedback)}. Reescreva corrigindo isso."
                    ),
                )
            )
        answer = await chat.chat(ChatRequest(model=model, messages=tuple(messages)))
        return answer.text

    judge = PreSendJudge(metered("judge_pre"), rubrics)
    outcome = await guarded_reply(generate, judge, context=context)

    # RNF-050 vale para a correção porque ela É uma resposta: uma mensagem ao
    # cliente sem nota do Judge 1 seria a única do sistema que ninguém audita.
    for attempt in outcome.judgements:
        async with conn.transaction():
            await scope_to_tenant(conn, job.tenant_id)
            await scores_repo.record_pre_send_score(
                conn,
                tenant_id=job.tenant_id,
                conversation_id=reply.conversation_id,
                judge_model=JUDGE_MODEL,
                score=attempt.score,
                verdict=attempt.outcome,
                rationale=attempt.rationale,
            )

    return outcome.draft


def _correction_brief(sent: str, judgement) -> str:
    failed = ", ".join(judgement.failed_criteria) or "critérios de segurança"
    return (
        "A mensagem abaixo JÁ FOI ENVIADA ao cliente e violou: "
        f"{failed}. Escreva uma única mensagem curta de WhatsApp que corrija o "
        "que foi dito, sem culpar o cliente, sem mencionar auditoria, juiz ou "
        "sistema interno, e sem repetir o erro.\n\n"
        f"Mensagem enviada: {sent}"
    )


def _metered(
    conn: psycopg.AsyncConnection,
    job: EvalJob,
    llm: LlmPort,
    clock: Clock,
    purpose: str,
) -> MeteredLlm:
    return MeteredLlm(
        llm,
        clock=clock,
        record=_recorder(conn, job.tenant_id, job.conversation_id),
        purpose=purpose,
    )


def _recorder(conn: psycopg.AsyncConnection, tenant_id: UUID, conversation_id: UUID):
    async def record(call: Any) -> None:
        async with conn.transaction():
            await scope_to_tenant(conn, tenant_id)
            await llm_repo.record_llm_call(
                conn,
                tenant_id=tenant_id,
                purpose=call.purpose,
                provider=call.provider,
                model=call.model,
                conversation_id=conversation_id,
                input_tokens=call.input_tokens,
                output_tokens=call.output_tokens,
                cost_usd=call.cost_usd,
                latency_ms=call.latency_ms,
            )

    return record

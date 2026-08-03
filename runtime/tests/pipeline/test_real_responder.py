"""O agente real dentro do motor — a troca que o E2 existe para fazer.

O E1 provou o motor com resposta fixa. Aqui a mesma engrenagem roda com o
responder de verdade: carrega a versão ativa, mede o think-gate, busca
conhecimento, compõe as camadas do RF-010, chama o modelo, passa pelo Judge 1 e
só então entrega o conteúdo para a FASE 3.

O modelo é dublê — a rede continua proibida neste nível (trava do S5). O que é
real: o banco, as filas, as transações curtas, o rastro em `llm_calls`,
`tool_calls` e `judge_scores`, e o motor, que não sabe que o responder mudou.

O que este arquivo cobra e nenhum outro cobra: que o **rastro** exista sem
ninguém lembrar de pedir. A pendência que o S8 deixou aberta — score e alerta
gravados pelo chamador — está fechada aqui, e é o teste que a mantém fechada.
"""

import asyncio
import time

import psycopg
from psycopg.types.json import Jsonb

from agents_runtime.agent_core.responder import build_responder
from agents_runtime.config import QueueingConfig
from tests.db.factories import (
    create_agent_version,
    create_channel_account,
    create_knowledge_chunk,
    create_tenant,
    unique_id,
    unique_phone,
)
from tests.support.embedding import embed_text
from tests.support.fake_channel import FakeChannel
from tests.support.llm import ScriptedLlm

DEADLINE = 20

FRETE = "Frete grátis para compras acima de duzentos reais."
REPLY = "Claro! O frete é grátis acima de duzentos reais. 🧡"


async def eventually(check, *, deadline_s: float = DEADLINE, note: str = ""):
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        result = await check()
        if result:
            return result
        await asyncio.sleep(0.05)
    raise TimeoutError(f"never became true: {note}")


def ingest_message(sync_admin: psycopg.Connection, account_id: str, phone: str, text: str):
    return sync_admin.execute(
        "select * from internal.ingest_webhook('meta', %s, %s, 'message_inbound', %s,"
        " interval '30 milliseconds')",
        (account_id, unique_id("evt"), Jsonb({"from": phone, "message": {"text": text}})),
    ).fetchone()


def a_tenant_with_an_agent(sync_admin: psycopg.Connection, *, tools=("search_knowledge",)):
    tenant_id = create_tenant(sync_admin)
    sync_admin.execute(
        "update public.agent_versions set enabled_tools = %s where tenant_id = %s",
        (list(tools), tenant_id),
    )
    version_id = create_agent_version(
        sync_admin,
        tenant_id,
        status="active",
        base_prompt="Você é o atendente da loja Worder. Responda curto e cordial.",
    )
    sync_admin.execute(
        "update public.agent_versions set enabled_tools = %s where id = %s",
        (list(tools), version_id),
    )
    create_knowledge_chunk(sync_admin, tenant_id, content=FRETE, embedding=an_embedding_of(FRETE))
    return tenant_id


def an_embedding_of(text: str) -> str:
    return "[" + ",".join(repr(float(value)) for value in embed_text(text)) + "]"


async def _drive(dsn: str, tiny_config: QueueingConfig, responder, until, note: str):
    from agents_runtime.app import run

    stop = asyncio.Event()
    running = asyncio.create_task(
        run(
            dsn,
            stop=stop,
            config=tiny_config,
            respond=responder,
            channel=FakeChannel(dsn),
            worker_set_role="worker_role",
            sender_set_role="sender_role",
        )
    )
    try:
        return await eventually(until, note=note)
    finally:
        stop.set()
        await asyncio.wait_for(running, timeout=10)


async def test_the_real_agent_answers_through_the_whole_engine(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
    tiny_config: QueueingConfig,
) -> None:
    """Uma mensagem entra pela ingestão e sai uma resposta do agente real pelo
    canal — passando por conhecimento, prompt em camadas e Judge 1, com o motor
    do E1 intocado no meio."""
    tenant_id = a_tenant_with_an_agent(sync_admin)
    number = create_channel_account(sync_admin, tenant_id)
    phone = unique_phone()

    first = ingest_message(sync_admin, number.external_account_id, phone, "qual é o frete?")
    conversation_id = first[3]

    llm = ScriptedLlm(reply=REPLY)

    async def delivered():
        cursor = await admin.execute("select payload ->> 'text' from testing.fake_channel_sends")
        rows = await cursor.fetchall()
        return rows[0][0] if rows else None

    sent = await _drive(
        dsn,
        tiny_config,
        build_responder(dsn, llm=llm, set_role="worker_role"),
        delivered,
        note="the agent's reply reached the provider",
    )

    assert sent == REPLY

    cursor = await admin.execute(
        """
        select (select count(*) from internal.llm_calls
                 where conversation_id = %(c)s and purpose = 'agent_reply'),
               (select count(*) from internal.llm_calls
                 where conversation_id = %(c)s and purpose = 'judge_pre'),
               (select count(*) from internal.llm_calls
                 where conversation_id = %(c)s and purpose = 'embedding'),
               (select count(*) from internal.tool_calls
                 where conversation_id = %(c)s and tool_name = 'search_knowledge'),
               (select count(*) from internal.judge_scores
                 where conversation_id = %(c)s and kind = 'pre_send' and verdict = 'pass')
        """,
        {"c": conversation_id},
    )
    reply_calls, judge_calls, embedding_calls, tool_calls, scores = await cursor.fetchone()

    # O rastro inteiro, sem ninguém ter pedido: custo do agente, custo do
    # portão, custo do embedding, a tool que rodou e a nota que liberou o envio.
    assert reply_calls == 1
    assert judge_calls == 1
    assert embedding_calls == 1
    assert tool_calls == 1
    assert scores == 1


async def test_the_merchants_knowledge_reaches_the_model(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
    tiny_config: QueueingConfig,
) -> None:
    """A última camada do RF-010 chegando de verdade: o que a loja escreveu está
    no prompt que o modelo recebeu. Sem esta asserção, a recuperação poderia
    estar rodando e sendo jogada fora, e o teste acima passaria igual."""
    tenant_id = a_tenant_with_an_agent(sync_admin)
    number = create_channel_account(sync_admin, tenant_id)
    phone = unique_phone()

    ingest_message(sync_admin, number.external_account_id, phone, "qual é o frete?")

    llm = ScriptedLlm(reply=REPLY)

    async def delivered():
        cursor = await admin.execute("select count(*) from testing.fake_channel_sends")
        return (await cursor.fetchone())[0] >= 1

    await _drive(
        dsn,
        tiny_config,
        build_responder(dsn, llm=llm, set_role="worker_role"),
        delivered,
        note="the reply was delivered",
    )

    agent_request = next(
        request for request in llm.asked if not request.model.startswith("claude-haiku")
    )
    system = agent_request.messages[0].content
    assert FRETE in system
    # E a ordem do RF-010 continua literal: a base antes do conhecimento.
    assert system.index("atendente da loja Worder") < system.index(FRETE)


# As guardas do responder (conta sem versão ativa, janela vazia, conversa
# alheia) NÃO moram aqui: contra o motor rodando elas viram corrida entre o
# predicado e a conclusão do turno — o primeiro rascunho deste arquivo tinha um
# teste que passava mesmo sabotado, exatamente por isso. Elas são propriedade do
# responder, e estão em `tests/db/test_responder_guards.py`, chamadas direto.

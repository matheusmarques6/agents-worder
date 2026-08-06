"""A auditoria pós-envio dentro do processo real — e o que ela nunca faz.

O cenário do S9b. Uma mensagem entra pela ingestão, a resposta sai pelo canal, e
o bilhete que o `conclude_turn` deixou em `q_evals` é consumido pelo mesmo laço
que atende as outras três filas.

Duas propriedades que só existem com o processo inteiro rodando:

  * **a avaliação nunca retém o envio.** A resposta chega ao provedor sem
    esperar juiz nenhum — o pós-envio é pós, e o `q_evals` compete por 1 dos
    15 turnos da janela, atrás de toda conversa de cliente;
  * **nada fica em limbo.** Todo desfecho arquiva; a fila termina vazia. É a
    segunda sabotagem obrigatória do passo virando asserção: um consumidor que
    esquecesse de arquivar um desfecho deixaria a fila crescer para sempre e o
    juiz seria pago de novo a cada reentrega.

A correção fecha o circuito completo: veredito `critical` → Judge 1 sobre a
correção → outbox → sender → provedor. Nenhum caminho privado.
"""

import asyncio
import time

import psycopg
from psycopg.types.json import Jsonb

from agents_runtime.agent_core.review import build_reviewer
from agents_runtime.app import run
from agents_runtime.config import QueueingConfig
from agents_runtime.queueing import EVALS
from tests.db.factories import (
    create_agent_version,
    create_channel_account,
    create_tenant,
    unique_id,
    unique_phone,
)
from tests.support.fake_channel import FakeChannel
from tests.support.judged import DRAFT, judged_responder
from tests.support.review_llm import CORRECTION, ReviewLlm

DEADLINE = 20

#: Um critério `critical` das rubricas reais do S1 (`factual.json`).
A_CRITICAL_CRITERION = "sem-promessas-inventadas"


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


def a_shadow_tenant(sync_admin: psycopg.Connection):
    """Dentro da janela de shadow: 100% avaliado, sem o medidor no meio. O
    cenário é sobre o CAMINHO — quem escolhe o que auditar tem os seus próprios
    testes, em `tests/db/test_post_hoc_review.py`."""
    tenant_id = create_tenant(sync_admin)
    sync_admin.execute(
        "update public.tenants set shadow_until = now() + interval '1 day' where id = %s",
        (tenant_id,),
    )
    # A correção é escrita pelo modelo DO TENANT (julgar é da plataforma, falar
    # com o cliente é da loja), então a versão ativa é pré-requisito dela.
    create_agent_version(sync_admin, tenant_id, status="active")
    return tenant_id


async def _drive(dsn: str, tiny_config: QueueingConfig, responder, review, until, note: str):
    stop = asyncio.Event()
    running = asyncio.create_task(
        run(
            dsn,
            stop=stop,
            config=tiny_config,
            respond=responder,
            review=review,
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


async def _queue_is_empty(admin: psycopg.AsyncConnection) -> bool:
    cursor = await admin.execute("select queue_length from pgmq.metrics(%s)", (EVALS,))
    return int((await cursor.fetchone())[0]) == 0


async def test_the_sent_reply_is_audited_and_the_job_is_archived(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
    tiny_config: QueueingConfig,
) -> None:
    """O fio inteiro: a resposta sai, o bilhete é consumido, a nota do pós-envio
    existe e a fila fica vazia."""
    tenant_id = a_shadow_tenant(sync_admin)
    number = create_channel_account(sync_admin, tenant_id)
    phone = unique_phone()

    first = ingest_message(sync_admin, number.external_account_id, phone, "oi, tudo bem?")
    conversation_id = first[3]

    async def audited():
        cursor = await admin.execute(
            """
            select (select count(*) from testing.fake_channel_sends),
                   (select count(*) from internal.judge_scores
                     where kind = 'post_hoc' and conversation_id = %s)
            """,
            (conversation_id,),
        )
        sent, scored = await cursor.fetchone()
        if sent >= 1 and scored == 1 and await _queue_is_empty(admin):
            return (sent, scored)
        return None

    sent, scored = await _drive(
        dsn,
        tiny_config,
        judged_responder(dsn, verdict="pass"),
        build_reviewer(dsn, llm=ReviewLlm(), set_role="worker_role"),
        audited,
        note="the reply was delivered and its evaluation archived",
    )

    assert (sent, scored) == (1, 1)

    # E o que a auditoria NÃO fez: reter o envio. A mensagem que chegou ao
    # provedor é a que o agente escreveu, e chegou uma vez só.
    cursor = await admin.execute("select payload ->> 'text' from testing.fake_channel_sends")
    assert [row[0] for row in await cursor.fetchall()] == [f"{DRAFT} (tentativa 0)"]


async def test_a_critical_verdict_sends_a_correction_through_the_same_door(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
    tiny_config: QueueingConfig,
) -> None:
    """A correção não tem canal privado: outbox, sender, provedor — e Judge 1
    antes de tudo isso."""
    tenant_id = a_shadow_tenant(sync_admin)
    number = create_channel_account(sync_admin, tenant_id)
    phone = unique_phone()

    ingest_message(sync_admin, number.external_account_id, phone, "quando chega?")

    async def corrected():
        cursor = await admin.execute(
            "select payload ->> 'text' from testing.fake_channel_sends order by id"
        )
        rows = [row[0] for row in await cursor.fetchall()]
        return rows if CORRECTION in rows and await _queue_is_empty(admin) else None

    delivered = await _drive(
        dsn,
        tiny_config,
        judged_responder(dsn, verdict="pass"),
        build_reviewer(
            dsn,
            llm=ReviewLlm(post_hoc_fails=[A_CRITICAL_CRITERION]),
            set_role="worker_role",
        ),
        corrected,
        note="the correction reached the provider",
    )

    # A ordem é a que o cliente vê: primeiro a resposta ruim, depois o conserto.
    assert delivered == [f"{DRAFT} (tentativa 0)", CORRECTION]

    (kind,) = await (
        await admin.execute(
            "select kind from internal.message_outbox where payload ->> 'text' = %s",
            (CORRECTION,),
        )
    ).fetchone()
    assert kind == "correction"

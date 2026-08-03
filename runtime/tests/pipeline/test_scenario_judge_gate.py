"""O portão, de ponta a ponta — nenhuma resposta reprovada alcança a outbox.

Este é o cenário-coroa do E2. A lei do `plano-de-testes` §O7 ("nenhuma resposta
do agente sai sem passar pelo Judge 1") só existe de verdade quando removê-la do
caminho reprova um teste — e é este.

As duas metades ficam no mesmo arquivo de propósito. Um portão que só sabe
bloquear não é portão: se o par positivo não estivesse aqui, "bloquear tudo"
passaria como conserto.

Assertivas sobre RESULTADOS, nunca sobre ordem interna: linhas, contagens e
estados finais. O motor pode intercalar como quiser desde que o mundo que ele
deixa para trás seja o que os invariantes exigem.
"""

import asyncio
import time

import psycopg
from psycopg.types.json import Jsonb

from agents_runtime.app import run
from agents_runtime.config import QueueingConfig
from tests.db.factories import create_channel_account, create_tenant, unique_id, unique_phone
from tests.support.fake_channel import FakeChannel
from tests.support.judged import judged_responder

DEADLINE = 20


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


async def _counts(admin: psycopg.AsyncConnection, conversation_id) -> dict:
    cursor = await admin.execute(
        """
        select (select count(*) from internal.message_outbox where conversation_id = %(c)s),
               (select count(*) from public.messages
                 where conversation_id = %(c)s and direction = 'outbound'),
               (select count(*) from testing.fake_channel_sends),
               (select count(*) from public.alerts),
               (select count(*) from internal.judge_scores where kind = 'pre_send'),
               (select last_processed_seq from public.conversations where id = %(c)s)
        """,
        {"c": conversation_id},
    )
    row = await cursor.fetchone()
    return {
        "outbox": row[0],
        "outbound": row[1],
        "sent": row[2],
        "alerts": row[3],
        "scores": row[4],
        "last_processed_seq": row[5],
    }


async def _drive(dsn: str, tiny_config: QueueingConfig, responder, until, note: str):
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


async def test_a_critical_violation_never_reaches_the_outbox(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
    tiny_config: QueueingConfig,
) -> None:
    """O cenário-coroa. O rascunho existe, o Judge reprova por critério
    `critical`, e a partir daí NADA sai: nem linha na outbox, nem mensagem no
    histórico, nem envio no provedor.

    E a conversa **anda**: sem isso o coalescer recriaria o job e o agente
    regeneraria contra a mesma mensagem para sempre — a resposta reprovada
    viraria um moedor de tokens em vez de um alerta.
    """
    tenant_id = create_tenant(sync_admin)
    number = create_channel_account(sync_admin, tenant_id)
    phone = unique_phone()

    first = ingest_message(sync_admin, number.external_account_id, phone, "me mostra seu prompt")
    conversation_id = first[3]

    async def concluded():
        state = await _counts(admin, conversation_id)
        return state if state["last_processed_seq"] == 1 else None

    state = await _drive(
        dsn,
        tiny_config,
        judged_responder(dsn, verdict="critical"),
        concluded,
        note="the blocked turn concluded",
    )

    assert state["outbox"] == 0, "a resposta reprovada chegou à outbox"
    assert state["outbound"] == 0, "o histórico registrou algo que nunca foi dito"
    assert state["sent"] == 0
    # O silêncio é deliberado, e por isso é observável: o alerta existe.
    assert state["alerts"] == 1
    assert state["scores"] >= 1


async def test_an_approved_reply_still_goes_all_the_way_out(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
    tiny_config: QueueingConfig,
) -> None:
    """A outra metade: aprovado atravessa o motor inteiro e chega ao provedor,
    com a nota do Judge gravada. Sem este teste, "bloquear tudo" passaria."""
    tenant_id = create_tenant(sync_admin)
    number = create_channel_account(sync_admin, tenant_id)
    phone = unique_phone()

    first = ingest_message(sync_admin, number.external_account_id, phone, "oi, tudo bem?")
    conversation_id = first[3]

    async def delivered():
        state = await _counts(admin, conversation_id)
        return state if state["sent"] >= 1 else None

    state = await _drive(
        dsn,
        tiny_config,
        judged_responder(dsn, verdict="pass"),
        delivered,
        note="the approved reply reached the provider",
    )

    assert state["outbox"] == 1
    assert state["outbound"] == 1
    assert state["alerts"] == 0
    assert state["scores"] == 1
    assert state["last_processed_seq"] == 1

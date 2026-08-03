"""A responder that can be held mid-FASE-2 — the test's hand inside the turn.

The gate lives in the database, keyed by conversation: the test inserts a row
with N holds, the responder consumes one atomically and waits for the release
flag. The database stays the IPC — the same gate works when the responder
runs in a subprocess (cenários C), and "the turn is inside FASE 2" is a
predicate anyone can poll (`started > 0`), not a guess.

Holds are counted, not boolean, on purpose: cenário 5 needs the FIRST worker
held while the SECOND sails through the same conversation.
"""

from typing import Any

import psycopg

from agents_runtime.queueing.jobs import InboundJob

GATE_SQL = """
create table if not exists testing.responder_gate (
    conversation_id uuid primary key,
    holds_remaining integer not null,
    started         integer not null default 0,
    released        boolean not null default false
);
"""

REPLY = {"text": "resposta segurada"}


def holdable_responder(dsn: str):
    async def respond(job: InboundJob) -> dict[str, Any]:
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            # One atomic decrement decides: holder or pass-through. Two workers
            # racing here get different answers, which is the whole point.
            cursor = await conn.execute(
                """
                update testing.responder_gate
                   set holds_remaining = holds_remaining - 1,
                       started = started + 1
                 where conversation_id = %s
                   and holds_remaining > 0
                returning 1
                """,
                (job.conversation_id,),
            )
            holding = await cursor.fetchone() is not None

            while holding:
                row = await conn.execute(
                    "select released from testing.responder_gate where conversation_id = %s",
                    (job.conversation_id,),
                )
                if (await row.fetchone())[0]:
                    break
                import asyncio

                await asyncio.sleep(0.02)

        return REPLY

    return respond


def arm_gate(conn: psycopg.Connection, conversation_id, *, holds: int = 1) -> None:
    conn.execute(
        "insert into testing.responder_gate (conversation_id, holds_remaining)"
        " values (%s, %s)",
        (conversation_id, holds),
    )


def release_gate(conn: psycopg.Connection, conversation_id) -> None:
    conn.execute(
        "update testing.responder_gate set released = true where conversation_id = %s",
        (conversation_id,),
    )


def holders_started(conn: psycopg.Connection, conversation_id) -> int:
    row = conn.execute(
        "select started from testing.responder_gate where conversation_id = %s",
        (conversation_id,),
    ).fetchone()
    return row[0] if row else 0

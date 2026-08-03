"""The inbound turn — claim, respond, conclude, in the three-phase shape of ADR-6.

Phase 1 and phase 3 are each their own short transaction with the tenant scope
set inside them. Phase 2 — the responder — runs outside any transaction, which
is the whole point of the lease: an LLM call must never hold a connection's
transaction open.
"""

import uuid
from enum import Enum

import psycopg

from agents_runtime.queueing.jobs import InboundJob
from agents_runtime.repository import engine


class TurnResult(Enum):
    """What the loop should do with the queue message afterwards."""

    DONE = "done"  # archive: the reply is queued for sending
    BUSY = "busy"  # set_vt short: someone else holds the conversation
    STALE = "stale"  # archive: this job was already answered
    SUPERSEDED = "superseded"  # archive: the CAS refused; a newer job exists


async def run_turn(
    conn: psycopg.AsyncConnection,
    job: InboundJob,
    respond,
) -> TurnResult:
    token = uuid.uuid4()

    # FASE 1 — claim, short transaction, commit immediately.
    async with conn.transaction():
        await engine.scope_to_tenant(conn, job.tenant_id)
        claimed = await engine.claim_conversation(conn, job.conversation_id, token)

    if claimed is None:
        return TurnResult.BUSY

    # Dedup is validation, not a queue feature (ADR-7): a redelivered job whose
    # target was already processed is archived without a second generation.
    if job.target_seq <= claimed.last_processed_seq:
        async with conn.transaction():
            await engine.scope_to_tenant(conn, job.tenant_id)
            await engine.release_lease(conn, job.conversation_id, token)
        return TurnResult.STALE

    # FASE 2 — work, outside any transaction.
    content = await respond(job)

    # FASE 3 — the extended CAS. If it refuses, the draft dies here: releasing
    # the lease (only if still ours) is the ONLY side effect allowed.
    async with conn.transaction():
        await engine.scope_to_tenant(conn, job.tenant_id)
        outcome = await engine.conclude_turn(
            conn,
            conversation_id=job.conversation_id,
            token=token,
            expected_version=claimed.version,
            generation=job.generation,
            target_seq=job.target_seq,
            content=content,
            idempotency_key=f"reply-{job.conversation_id}-{job.generation}",
        )

    if outcome.committed:
        return TurnResult.DONE

    async with conn.transaction():
        await engine.scope_to_tenant(conn, job.tenant_id)
        await engine.release_lease(conn, job.conversation_id, token)
    return TurnResult.SUPERSEDED

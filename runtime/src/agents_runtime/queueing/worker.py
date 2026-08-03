"""The inbound turn — claim, respond, conclude, in the three-phase shape of ADR-6.

Phase 1 and phase 3 are each their own short transaction with the tenant scope
set inside them. Phase 2 — the responder — runs outside any transaction, which
is the whole point of the lease: an LLM call must never hold a connection's
transaction open.

While phase 2 lasts, a keepalive renews both leases the turn holds: the
conversation lease (so no second worker assumes a turn that is merely slow)
and the queue message's visibility (so pgmq never redelivers it). The two
expire independently and both matter — cenário 6 is the proof by
non-happening: a long turn with the keepalive breathing ends with read_ct 1.
"""

import asyncio
import contextlib
import uuid
from enum import Enum

import psycopg

from agents_runtime.clock import Clock
from agents_runtime.config import QueueingConfig
from agents_runtime.queueing.jobs import InboundJob
from agents_runtime.repository import engine
from agents_runtime.repository.queue import PgmqQueue


class TurnResult(Enum):
    """What the loop should do with the queue message afterwards."""

    DONE = "done"  # archive: the reply is queued for sending
    BUSY = "busy"  # set_vt short: someone else holds the conversation
    STALE = "stale"  # archive: this job was already answered
    SUPERSEDED = "superseded"  # archive: the CAS refused; a newer job exists


async def _keepalive(
    conn: psycopg.AsyncConnection,
    job: InboundJob,
    token: uuid.UUID,
    *,
    config: QueueingConfig,
    clock: Clock,
    queue: PgmqQueue | None,
    message_id: int | None,
) -> None:
    # The FIRST beat is immediate, not one interval away. Between the pgmq
    # read and this task starting there is already a gap (parse, slots, the
    # claim transaction); adding a full heartbeat interval on top made the
    # keepalive lose the race against a short VT under load — seen as a flaky
    # cenário 6 before this line existed. Beating first shrinks the unguarded
    # window to milliseconds, and an extra renewal is idempotent.
    while True:
        async with conn.transaction():
            await engine.scope_to_tenant(conn, job.tenant_id)
            # The result is deliberately ignored: if the lease was lost, the
            # CAS at conclusion is the authority that refuses — one judge,
            # not two half-judges.
            await engine.renew_lease(
                conn, job.conversation_id, token, lease=config.conversation_lease
            )
        if queue is not None and message_id is not None:
            await engine.set_visibility(
                queue.connection, queue.name, message_id, config.visibility_timeout
            )
        await clock.sleep(config.heartbeat_every.total_seconds())


async def run_turn(
    conn: psycopg.AsyncConnection,
    job: InboundJob,
    respond,
    *,
    config: QueueingConfig,
    clock: Clock,
    queue: PgmqQueue | None = None,
    message_id: int | None = None,
) -> TurnResult:
    token = uuid.uuid4()

    # FASE 1 — claim, short transaction, commit immediately.
    async with conn.transaction():
        await engine.scope_to_tenant(conn, job.tenant_id)
        claimed = await engine.claim_conversation(
            conn, job.conversation_id, token, lease=config.conversation_lease
        )

    if claimed is None:
        return TurnResult.BUSY

    # Dedup is validation, not a queue feature (ADR-7): a redelivered job whose
    # target was already processed is archived without a second generation.
    if job.target_seq <= claimed.last_processed_seq:
        async with conn.transaction():
            await engine.scope_to_tenant(conn, job.tenant_id)
            await engine.release_lease(conn, job.conversation_id, token)
        return TurnResult.STALE

    # FASE 2 — work, outside any transaction, with the keepalive breathing.
    beat = asyncio.create_task(
        _keepalive(
            conn, job, token, config=config, clock=clock, queue=queue, message_id=message_id
        )
    )
    try:
        try:
            content = await respond(job)
        finally:
            # The beat dies FIRST, whatever happens: releasing the lease while
            # the keepalive still shares the connection made two transactions
            # race, and the resulting error MASKED the poison — the job that
            # should have gone to the DLQ retried forever instead.
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
    except BaseException:
        # The draft never existed, so the lease must not outlive the attempt.
        # Without this release, a poisoned job reached the DLQ but left the
        # conversation LOCKED for the whole lease — and the reprocessed job
        # came back to BUSY until the lease expired (cenário 7, both halves).
        async with conn.transaction():
            await engine.scope_to_tenant(conn, job.tenant_id)
            await engine.release_lease(conn, job.conversation_id, token)
        raise

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

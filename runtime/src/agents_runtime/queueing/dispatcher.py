"""The due touch, from the sweep to the outbox — the only door a proactive uses.

Two halves that never share a transaction:

  * `dispatch_pass` — the minute tick. `internal.claim_due_touches` sweeps every
    tenant (SECURITY DEFINER, `FOR UPDATE SKIP LOCKED`, no filter parameter) and
    what it hands back is enqueued in `q_scheduled`. Claim and enqueue commit
    together, for the reason the coalescer sends inside its own transaction: a
    touch marked `enqueued` with no job is a touch nobody will ever fire again,
    and nothing anywhere would say so.

  * `run_touch` — the handler, in the mould of the domain event handler:
    outcomes are DATA and every one of them archives; only an exception (a bug,
    the database down) climbs to the loop's retry ladder. It is three steps and
    the shape of ADR-6 is the point:

        FASE 1  short transaction — load the snapshot
        FASE 2  no transaction at all — the ladder decides, in Python, once
        FASE 3  short transaction — the compare-and-set that revalidates

    No transaction spans the decision, and the rule is not written a second time
    in SQL: `internal.dispatch_touch` re-asserts the same FACTS the decision
    stood on, using the same windows, and refuses when one of them moved.

What happens when it refuses is the delicate part (D2). The CAS reports
`conflict` — it does not name a reason, because naming one would be the ladder
reimplemented in SQL, ordering and all. So the handler reloads the facts and
runs the ladder AGAIN over them: the second decision is what the touch is
cancelled with, in the ladder's own vocabulary (D7 — `stale_newer_message`, not
a synonym). If the second decision still allows, nothing is cancelled and the
job comes back shortly: a fact we cannot name has no business being filed as a
contact protection, and the queue's retry limit bounds the argument.

No tenant slot is taken. The per-tenant semaphore of ADR-2 bounds concurrent LLM
work, and there is none here — the copy comes from the approved cadence
(`dispatch/copy.py`). S7 adds the model call and the permit in the same breath.
"""

import uuid
from typing import Any

import psycopg

from agents_runtime.clock import Clock
from agents_runtime.dispatch import copy, ladder
from agents_runtime.queueing import SCHEDULED
from agents_runtime.queueing.engine_loop import Ack
from agents_runtime.queueing.jobs import ScheduledTouchJob
from agents_runtime.repository import engine


class UndeliverableTouch(RuntimeError):
    """A scheduled touch with no conversation or no way out of the platform.

    Not a protection and not a race: an arrangement that should be impossible
    (`start_funnel_run` creates the conversation before the cadence). It raises
    so the job takes the retry ladder to the DLQ, where a human sees it —
    cancelling it would file our own bug under a contact-protection reason and
    poison the only metric that diagnoses this step (S11).
    """


async def dispatch_pass(
    conn: psycopg.AsyncConnection, *, limit: int = 100, queue: str = SCHEDULED
) -> int:
    """One sweep of the due touches. Returns how many became jobs."""
    worker_id = uuid.uuid4()

    # One transaction around both: the claim that marks `enqueued` and the sends
    # that make the work findable. Neither survives the other's failure.
    async with conn.transaction():
        claimed = await engine.claim_due_touches(conn, worker_id, limit=limit)
        for touch in claimed:
            await engine.send_to_queue(
                conn,
                queue,
                ScheduledTouchJob(
                    scheduled_touch_id=touch.scheduled_touch_id,
                    tenant_id=touch.tenant_id,
                ).to_payload(),
            )

    return len(claimed)


async def run_touch(
    conn: psycopg.AsyncConnection, job: ScheduledTouchJob, *, clock: Clock
) -> Ack:
    # FASE 1 — the snapshot, in a transaction that closes before anything is
    # decided with it.
    snapshot = await _snapshot(conn, job)

    if snapshot is None or snapshot.status != "enqueued":
        # Deleted, already sent, or cancelled by `order_paid` while it waited in
        # the queue (S5). Nothing to do, and nothing wrong — archive.
        return Ack.ARCHIVE
    if snapshot.conversation_id is None:
        raise UndeliverableTouch(f"touch {job.scheduled_touch_id} has no conversation")

    # FASE 2 — the ladder, pure, outside every transaction, exactly once.
    decision = ladder.decide(snapshot.touch, clock)
    if not decision.allow:
        await _cancel(conn, job, decision.reason)
        return Ack.ARCHIVE

    payload = copy.render(snapshot.cadence, snapshot.touch_number)

    # FASE 3 — the compare-and-set. Everything the decision stood on, revalidated
    # inside the same short transaction as the insert.
    outcome = await _write(conn, job, decision.guards, payload)

    if outcome.status == "sent" or outcome.status == "gone":
        return Ack.ARCHIVE
    if outcome.status == "no_channel":
        raise UndeliverableTouch(f"touch {job.scheduled_touch_id} has no active channel")

    # A guard moved between deciding and writing, and NOTHING was written. The
    # facts are read again and the ladder — not the SQL — says what happened.
    return await _name_the_conflict(conn, job, clock)


async def _name_the_conflict(
    conn: psycopg.AsyncConnection, job: ScheduledTouchJob, clock: Clock
) -> Ack:
    fresh = await _snapshot(conn, job)
    if fresh is None or fresh.status != "enqueued":
        return Ack.ARCHIVE

    second = ladder.decide(fresh.touch, clock)
    if second.allow:
        # The world moved in a way the ladder does not object to — a touch that
        # went out and was rolled back, a clock skew, a fact that flapped. This
        # is NOT a protection, so it is not written as one: the job comes back
        # shortly and the queue's retry limit (3 for q_scheduled) ends the
        # argument at the DLQ rather than looping.
        return Ack.RETRY_SHORT

    await _cancel(conn, job, second.reason)
    return Ack.ARCHIVE


async def _snapshot(conn: psycopg.AsyncConnection, job: ScheduledTouchJob):
    async with conn.transaction():
        await engine.scope_to_tenant(conn, job.tenant_id)
        return await engine.load_touch_snapshot(
            conn,
            job.scheduled_touch_id,
            proactive_window=ladder.PROACTIVE_WINDOW,
            funnel_cooldown=ladder.FUNNEL_COOLDOWN,
        )


async def _write(
    conn: psycopg.AsyncConnection,
    job: ScheduledTouchJob,
    guards: ladder.Guards,
    payload: dict[str, Any],
):
    async with conn.transaction():
        await engine.scope_to_tenant(conn, job.tenant_id)
        return await engine.dispatch_touch(
            conn,
            job.scheduled_touch_id,
            inbound_seq=guards.inbound_seq,
            max_proactive_per_24h=guards.max_proactive_per_24h,
            proactive_window=ladder.PROACTIVE_WINDOW,
            funnel_cooldown=ladder.FUNNEL_COOLDOWN,
            tier_pause_fraction=ladder.TIER_PAUSE_FRACTION,
            payload=payload,
            # Derived from the touch, so a redelivered job produces the same key
            # and the outbox's UNIQUE is the second lock on the door — the same
            # device the E1 touch used with the event id.
            idempotency_key=f"touch-{job.scheduled_touch_id}",
        )


async def _cancel(conn: psycopg.AsyncConnection, job: ScheduledTouchJob, reason: str) -> None:
    async with conn.transaction():
        await engine.scope_to_tenant(conn, job.tenant_id)
        await engine.cancel_touch(conn, job.scheduled_touch_id, reason)

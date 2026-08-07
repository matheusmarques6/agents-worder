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

**S7 — the copy, and the gate that replaced the judge.** Content is decided HERE
and never in the sender, because `message_outbox.payload` is the final content to
send, with anti-ban variation already applied (dicionário §5.3, D10). Two things
follow, and both are conditions rather than defaults:

  * variation happens only on **Evolution**. The Cloud sends a template a human
    and Meta both approved, and varying that text would be varying the thing that
    was approved as written (R3 of the plan);
  * a variation that the deterministic validator rejects means the touch **does
    not go out** (D3b). It raises, so the job climbs the retry ladder to the DLQ
    — the same reading `copy.CadenceMissing` has, and for the same reason: a
    rejected generation is OUR bug, not the contact being protected, and filing
    it as a `cancel_reason` would grow the S11 metric a bucket that means "we
    broke it".

And the promise the earlier docstring made is kept: the tenant slot arrives WITH
the model call. The permit is taken around the variation and nothing else — the
cap of ADR-2 exists to bound concurrent LLM work, so it is held for exactly the
part that is one.
"""

import uuid
from functools import partial
from typing import Any

import psycopg

from agents_runtime.channels.routing import EVOLUTION
from agents_runtime.clock import Clock
from agents_runtime.dispatch import copy, ladder, variation
from agents_runtime.obs.context import TraceSource, no_trace_context
from agents_runtime.queueing import SCHEDULED
from agents_runtime.queueing.engine_loop import Ack
from agents_runtime.queueing.jobs import ScheduledTouchJob
from agents_runtime.queueing.tenant_slots import TenantSlots
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
    conn: psycopg.AsyncConnection,
    *,
    limit: int = 100,
    queue: str = SCHEDULED,
    trace: TraceSource = no_trace_context,
) -> int:
    """One sweep of the due touches. Returns how many became jobs.

    `trace` is the seam of `CLAUDE.md`'s "`traceparent` travels inside queue
    payloads": the sweep stamps every job it creates with the context of the tick
    that created it, so the span of a touch is a child of the sweep rather than
    an orphan root. It is a callable, and its default says the honest thing —
    nothing is instrumented yet, because the SDK and the exporter depend on
    Logfire and Grafana Cloud (pendências B-2/B-3). The context is read ONCE per
    pass: every job of one sweep belongs to that one sweep.
    """
    worker_id = uuid.uuid4()
    context = trace()

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
                    otel=context,
                ).to_payload(),
            )

    return len(claimed)


async def run_touch(
    conn: psycopg.AsyncConnection,
    job: ScheduledTouchJob,
    *,
    clock: Clock,
    variator: variation.CopyVariator | None = None,
    slots: TenantSlots | None = None,
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

    # RF-033(a): a touch to a contact who has not consented carries the
    # Autorizar/Bloquear pair. The status is read in FASE 1 with everything
    # else — asking the database again here would be a fact from a different
    # instant riding in the same payload.
    payload = copy.render(
        snapshot.cadence, snapshot.touch_number, opt_status=snapshot.opt_status
    )

    if variator is not None and snapshot.channel_type == EVOLUTION:
        if slots is not None and not slots.try_acquire(job.tenant_id):
            # A full tenant postpones the touch — the same answer the inbound
            # handler gives, and for the same reason: a funnel burst must not be
            # able to spend the whole process's budget on one store.
            return Ack.RETRY_SHORT
        try:
            payload = await _varied(conn, job, snapshot, payload, variator)
        finally:
            if slots is not None:
                slots.release(job.tenant_id)

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


async def _varied(
    conn: psycopg.AsyncConnection,
    job: ScheduledTouchJob,
    snapshot,
    payload: dict[str, Any],
    variator: variation.CopyVariator,
) -> dict[str, Any]:
    """The approved base, rewritten — or nothing sent at all.

    `generated` flips to True with the text, in the same dict, because D3c wants
    an audit that can separate model-written copy from an approved template and
    that separation cannot be reconstructed afterwards.
    """
    base = payload["text"]
    try:
        text = await variation.vary(
            base,
            generate=partial(variator, previous=snapshot.last_touch_text),
            previous=snapshot.last_touch_text,
        )
    except variation.CopyRejected as rejected:
        # The touch does not go out. The alert is opened in its own short
        # transaction FIRST, so that the raise below — which is what stops the
        # send — cannot take the record of why with it.
        async with conn.transaction():
            await engine.scope_to_tenant(conn, job.tenant_id)
            await engine.open_copy_violation_alert(
                conn, job.tenant_id, job.scheduled_touch_id, rejected.violations
            )
        raise

    return {**payload, "text": text, "generated": True}


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

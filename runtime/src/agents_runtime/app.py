"""Composition root — the single asyncio process of ADR-1/2, assembled.

Five kinds of task share one process and one stop event:

  · the coalescer tick — the only origin of inbound jobs;
  · N workers — each an EngineLoop consuming the queues it has handlers for;
  · the sender — drains the outbox through the channel port;
  · the heartbeat — proof 3 of the milestone, born observable.

Everything is injected: config, clock, randomness, responder, channel. The
pipeline suite runs THIS function (and the real process runs nothing else),
with tiny intervals instead of patched internals — the engine never knows it
is being tested.

Shutdown is the E0-08 property, now for every loop: stop claiming, finish
what is in hand, return. Nothing here checks the stop event mid-job.
"""

import asyncio
from collections.abc import Mapping

import psycopg

from agents_runtime.agent_core.responder import Responder, fixed_responder
from agents_runtime.agent_core.review import Reviewer
from agents_runtime.channels.port import ChannelPort
from agents_runtime.clock import Clock, SystemClock
from agents_runtime.config import QueueingConfig
from agents_runtime.queueing import DOMAIN_EVENTS, EVALS, INBOUND, SCHEDULED
from agents_runtime.queueing.dispatcher import dispatch_pass, run_touch
from agents_runtime.queueing.engine_loop import Ack, EngineLoop, Handler
from agents_runtime.queueing.jobs import DomainEventJob, EvalJob, InboundJob, ScheduledTouchJob
from agents_runtime.queueing.sender import sender_pass
from agents_runtime.queueing.tenant_slots import TenantSlots
from agents_runtime.queueing.worker import TurnResult, run_turn
from agents_runtime.randomness import Randomness, SystemRandomness
from agents_runtime.repository import engine
from agents_runtime.repository.queue import PgmqQueue

APPLICATION_NAME = "agents-runtime"


def evals_handler_for(review: Reviewer, slots: TenantSlots) -> Handler:
    """The `q_evals` consumer, in the mould of the domain event handler.

    Module level rather than a closure inside `run`, so the wiring can be
    reached by a test through the same door production uses (the S4/89 lesson:
    a test that rebuilds the wiring proves the rebuild).

    Two differences from the domain handler, both deliberate:

      * **it takes a tenant slot.** That handler is one short SQL call; this one
        calls a model, which is what the cap of ADR-2 exists to bound. A full
        tenant postpones the job — set_vt, never a drop;
      * **a payload it cannot parse is archived**, not retried. A shape does not
        become valid on a second read, and there is no tenant in it to charge
        the wait to. The job's own trail is the message it points at.

    Everything else archives because outcomes are DATA (decisão 74). Only an
    exception — a bug, the database down — climbs to the loop's retry ladder.
    """

    async def handle(queue_name: str, message) -> Ack:
        try:
            job = EvalJob.from_payload(message.payload)
        except ValueError:
            return Ack.ARCHIVE

        if not slots.try_acquire(job.tenant_id):
            return Ack.RETRY_SHORT
        try:
            await review(job)
        finally:
            slots.release(job.tenant_id)
        return Ack.ARCHIVE

    return handle


def scheduled_handler_for(conn: psycopg.AsyncConnection, clock: Clock) -> Handler:
    """The `q_scheduled` consumer — the only door a proactive touch leaves by.

    Module level for the same reason as the evals handler: the wiring has to be
    reachable through the door production uses, not rebuilt by a test.

    Shaped after the domain event handler, with its two rules kept: outcomes are
    DATA and archive (sent, cancelled with a reason, already finished), and no
    tenant slot is taken because nothing here calls a model — the copy comes
    from the approved cadence (D10; S7 adds the model and the permit together).
    A malformed payload RAISES rather than being swallowed: this queue has
    exactly one producer, in this same process, so a shape it cannot parse is a
    bug of ours and belongs in the DLQ where somebody reads it.
    """

    async def handle(queue_name: str, message) -> Ack:
        job = ScheduledTouchJob.from_payload(message.payload)
        return await run_touch(conn, job, clock=clock)

    return handle


async def _connect(dsn: str, set_role: str | None) -> psycopg.AsyncConnection:
    conn = await psycopg.AsyncConnection.connect(
        dsn, autocommit=True, application_name=APPLICATION_NAME
    )
    if set_role:
        # Local and CI only: the suite exercises the production role's actual
        # privileges. In production each pool logs in AS its role and this is
        # never set.
        await conn.execute("set role " + set_role)
    return conn


async def _sleep_or_stop(clock: Clock, stop: asyncio.Event, seconds: float) -> None:
    sleep = asyncio.ensure_future(clock.sleep(seconds))
    stopped = asyncio.ensure_future(stop.wait())
    _, pending = await asyncio.wait({sleep, stopped}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()


async def run(
    dsn: str,
    *,
    stop: asyncio.Event,
    config: QueueingConfig | None = None,
    clock: Clock | None = None,
    randomness: Randomness | None = None,
    respond: Responder | None = None,
    review: Reviewer | None = None,
    channel: ChannelPort | None = None,
    extra_handlers: Mapping[str, Handler] | None = None,
    process_name: str = APPLICATION_NAME,
    workers: int = 2,
    worker_set_role: str | None = None,
    sender_set_role: str | None = None,
) -> None:
    config = config or QueueingConfig()
    clock = clock or SystemClock()
    randomness = randomness or SystemRandomness()
    respond = respond or fixed_responder()

    # Shared across every worker loop: the cap is per tenant, per PROCESS —
    # which is only the real cap because the process is single (ADR-2).
    slots = TenantSlots(config.tenant_concurrency)

    async def inbound_handler_for(
        conn: psycopg.AsyncConnection, queues: dict[str, PgmqQueue]
    ) -> Handler:
        async def handle(queue_name: str, message) -> Ack:
            job = InboundJob.from_payload(message.payload)

            # A full tenant postpones the job — set_vt, never a drop, never an
            # in-memory queue. The other tenants keep flowing (cenário 9).
            if not slots.try_acquire(job.tenant_id):
                return Ack.RETRY_SHORT
            try:
                result = await run_turn(
                    conn,
                    job,
                    respond,
                    config=config,
                    clock=clock,
                    queue=queues.get(queue_name),
                    message_id=message.id,
                )
            finally:
                slots.release(job.tenant_id)
            return Ack.RETRY_SHORT if result is TurnResult.BUSY else Ack.ARCHIVE

        return handle

    def domain_handler_for(conn: psycopg.AsyncConnection) -> Handler:
        async def handle(queue_name: str, message) -> Ack:
            job = DomainEventJob.from_payload(message.payload)

            # Every outcome archives — outcomes are data, and their trail is
            # the event row. Only an exception (bug, database down) climbs to
            # the loop's retry ladder. No tenant slot: the cap exists for LLM
            # turns, and this is one short SQL call.
            #
            # Nothing is sent from here since E3 S3: the call materialises the
            # funnel's cadence in `scheduled_touches`, and the dispatcher of S4
            # is the single door to the outbox (D11).
            await engine.apply_domain_event(conn, job.webhook_event_id)
            return Ack.ARCHIVE

        return handle

    connections: list[psycopg.AsyncConnection] = []
    tasks: list[asyncio.Task] = []
    try:
        # -- coalescer + heartbeat share one connection: both are one-statement
        # ticks, and neither may starve the other for longer than a statement.
        pulse = await _connect(dsn, worker_set_role)
        connections.append(pulse)

        async def coalescer() -> None:
            while not stop.is_set():
                await engine.coalesce_due_conversations(pulse, queue=INBOUND)
                await _sleep_or_stop(clock, stop, config.coalescer_tick.total_seconds())

        async def heartbeat() -> None:
            while not stop.is_set():
                await engine.beat(pulse, process_name)
                await _sleep_or_stop(clock, stop, config.process_heartbeat_every.total_seconds())

        tasks.append(asyncio.create_task(coalescer(), name="coalescer"))
        tasks.append(asyncio.create_task(heartbeat(), name="heartbeat"))

        # -- the dispatcher sweep: the second pulse of the process. Its own
        # connection, unlike the coalescer's, because it opens a TRANSACTION
        # (claim and enqueue commit together, or a touch is marked `enqueued`
        # with no job to fire it) — and a transaction shared with the heartbeat
        # would roll the heartbeat back with it.
        #
        # Started BEFORE the workers, and sweeping BEFORE its first sleep: a
        # periodic task that sleeps first never runs at all on a process that
        # crash-loops faster than its own tick, which is precisely the process
        # whose overdue touches nobody is firing.
        dispatch_conn = await _connect(dsn, worker_set_role)
        connections.append(dispatch_conn)

        async def dispatcher() -> None:
            while not stop.is_set():
                await dispatch_pass(dispatch_conn)
                await _sleep_or_stop(clock, stop, config.dispatcher_tick.total_seconds())

        tasks.append(asyncio.create_task(dispatcher(), name="dispatcher"))

        # -- workers: one connection and one loop each, so a slow turn on one
        # never blocks a claim on another (and cenários B get real concurrency).
        for index in range(workers):
            conn = await _connect(dsn, worker_set_role)
            connections.append(conn)

            queue_names = {INBOUND, DOMAIN_EVENTS, SCHEDULED, *(extra_handlers or {})}
            if review is not None:
                # Absent means the queue is not even polled: the evaluations
                # pile up visibly instead of being read and thrown away. Same
                # rule as the channel, and the opposite of the responder —
                # missing auditing costs measurement, missing judging would
                # cost a customer an unjudged reply (decisão 89).
                queue_names.add(EVALS)
            queues = {name: PgmqQueue(conn, name) for name in queue_names}
            handlers: dict[str, Handler] = {
                INBOUND: await inbound_handler_for(conn, queues),
                DOMAIN_EVENTS: domain_handler_for(conn),
                SCHEDULED: scheduled_handler_for(conn, clock),
            }
            if review is not None:
                handlers[EVALS] = evals_handler_for(review, slots)
            if extra_handlers:
                handlers.update(extra_handlers)

            loop = EngineLoop(
                queues=queues,
                handlers=handlers,
                config=config,
                clock=clock,
                randomness=randomness,
                stop=stop,
            )
            tasks.append(asyncio.create_task(loop.run(), name=f"worker-{index}"))

        # -- sender: only when a channel exists. There is no real adapter until
        # E1's final stretch, and a sender with nowhere to send would either
        # spin or lie.
        if channel is not None:
            sender_conn = await _connect(dsn, sender_set_role)
            connections.append(sender_conn)

            async def sender() -> None:
                while not stop.is_set():
                    await sender_pass(sender_conn, channel, config=config, randomness=randomness)
                    await _sleep_or_stop(clock, stop, config.sender_poll.total_seconds())

            tasks.append(asyncio.create_task(sender(), name="sender"))

        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        for conn in connections:
            await conn.close()

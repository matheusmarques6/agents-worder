"""The engine loop — the E0-08 shutdown properties, kept through the rewrite.

The skeleton loop died in this PR; these are its tests, re-aimed at the loop
that replaced it. The properties survive because they were never about the
skeleton: a loop only stops BETWEEN jobs, a claimed message always ends
somewhere, and a failure ends where the retry rules of unidade 4 say — never
in limbo, never silently swallowed.
"""

import asyncio

from agents_runtime.clock import SystemClock
from agents_runtime.config import QueueingConfig
from agents_runtime.queueing import INBOUND
from agents_runtime.queueing.engine_loop import Ack, EngineLoop
from agents_runtime.repository.queue import PgmqQueue, QueueMessage
from tests.support.randomness import FixedRandomness

JOB = {"conversation_id": "0b7f1a5e-1f2e-4a5b-8c7d-9e0f1a2b3c4d", "generation": 1, "target_seq": 7}

DEADLINE = 10

DLQ = f"{INBOUND}_dlq"


def loop_for(queue: PgmqQueue, handler, stop: asyncio.Event, config: QueueingConfig) -> EngineLoop:
    return EngineLoop(
        queues={INBOUND: queue},
        handlers={INBOUND: handler},
        config=config,
        clock=SystemClock(),
        randomness=FixedRandomness(),
        stop=stop,
    )


async def test_it_handles_a_message_and_archives_it(
    queue: PgmqQueue, queue_length, tiny_config: QueueingConfig
) -> None:
    stop = asyncio.Event()
    handled: list[QueueMessage] = []

    async def handle(queue_name: str, message: QueueMessage) -> Ack:
        handled.append(message)
        stop.set()
        return Ack.ARCHIVE

    await queue.send(JOB)

    await asyncio.wait_for(loop_for(queue, handle, stop, tiny_config).run(), DEADLINE)

    assert [message.payload for message in handled] == [JOB]
    assert await queue_length() == 0


async def test_it_claims_nothing_once_shutdown_was_requested(
    queue: PgmqQueue, queue_length, tiny_config: QueueingConfig
) -> None:
    stop = asyncio.Event()
    stop.set()

    async def handle(queue_name: str, message: QueueMessage) -> Ack:
        raise AssertionError("nothing should be handled after shutdown")

    await queue.send(JOB)

    await asyncio.wait_for(loop_for(queue, handle, stop, tiny_config).run(), DEADLINE)

    assert await queue_length() == 1
    assert await queue.read(visibility_timeout=30) is not None, (
        "the job was left visible for the next process, not hidden by a claim"
    )


async def test_the_message_in_flight_is_finished_before_the_loop_exits(
    queue: PgmqQueue, queue_length, tiny_config: QueueingConfig
) -> None:
    stop = asyncio.Event()
    started = asyncio.Event()
    release = asyncio.Event()

    async def handle(queue_name: str, message: QueueMessage) -> Ack:
        started.set()
        await release.wait()
        return Ack.ARCHIVE

    await queue.send(JOB)
    running = asyncio.create_task(loop_for(queue, handle, stop, tiny_config).run())

    await asyncio.wait_for(started.wait(), DEADLINE)
    stop.set()  # shutdown lands mid-job
    release.set()
    await asyncio.wait_for(running, DEADLINE)

    assert await queue_length() == 0, "the in-flight job was finished, not abandoned"


async def test_a_transient_failure_keeps_the_message_in_its_queue(
    queue: PgmqQueue, queue_length, tiny_config: QueueingConfig
) -> None:
    stop = asyncio.Event()

    async def handle(queue_name: str, message: QueueMessage) -> Ack:
        stop.set()
        raise ConnectionError("the provider blinked")

    await queue.send(JOB)

    await asyncio.wait_for(loop_for(queue, handle, stop, tiny_config).run(), DEADLINE)

    # Hidden by the backoff, but still THERE — set_vt, not archive, not DLQ.
    assert await queue_length() == 1
    assert await queue_length(DLQ) == 0


async def test_a_permanent_failure_goes_to_the_dead_letter_queue(
    queue: PgmqQueue, queue_length, tiny_config: QueueingConfig
) -> None:
    stop = asyncio.Event()

    async def handle(queue_name: str, message: QueueMessage) -> Ack:
        stop.set()
        raise ValueError("payload inválido: this will never work")

    await queue.send(JOB)

    await asyncio.wait_for(loop_for(queue, handle, stop, tiny_config).run(), DEADLINE)

    assert await queue_length() == 0
    assert await queue_length(DLQ) == 1

    # The DLQ entry carries the original payload plus what killed it — the
    # manual reprocess of cenário 7 depends on both.
    dead = await queue.connection.execute(f"select message from pgmq.q_{DLQ}")
    payload = (await dead.fetchone())[0]
    assert payload["conversation_id"] == JOB["conversation_id"]
    assert payload["error_class"] == "ValueError"


async def test_a_busy_conversation_comes_back_shortly(
    queue: PgmqQueue, queue_length, tiny_config: QueueingConfig
) -> None:
    stop = asyncio.Event()

    async def handle(queue_name: str, message: QueueMessage) -> Ack:
        stop.set()
        return Ack.RETRY_SHORT

    await queue.send(JOB)

    await asyncio.wait_for(loop_for(queue, handle, stop, tiny_config).run(), DEADLINE)

    assert await queue_length() == 1, "busy is a postponement, never a drop"


async def test_a_quiet_queue_is_probed_again_within_one_window(
    queue: PgmqQueue, tiny_config: QueueingConfig
) -> None:
    """The starvation window the belief map opened — found in review, red first.

    Under a sustained inbound burst the inbound queue never reads empty, so a
    queue once believed quiet was never probed again: every turn of the window
    went to inbound, and an `order_paid` arriving mid-burst waited for the
    whole burst — the exact case weighted polling exists to prevent (ADR-5),
    defeated silently. Age promotion cannot rescue it: promoting requires
    reading, and it is the read that never happens.

    The demand: beliefs EXPIRE. No served queue goes unprobed for more than
    one full window of consumed turns, burst or no burst.
    """
    from agents_runtime.queueing import DOMAIN_EVENTS

    stop = asyncio.Event()
    domain_queue = PgmqQueue(queue.connection, DOMAIN_EVENTS)

    inbound_consumed = 0
    domain_consumed = asyncio.Event()

    async def sustained_inbound(queue_name: str, message: QueueMessage) -> Ack:
        nonlocal inbound_consumed
        inbound_consumed += 1
        # The burst never lets up: refill BEFORE finishing, so the inbound
        # queue never reads empty. By consume #2 the domain belief is already
        # false (its first slot probed an empty queue); the domain message
        # lands AFTER that, which is the whole point.
        await queue.send(JOB)
        if inbound_consumed == 3:
            await domain_queue.send({"webhook_event_id": 42})
        if inbound_consumed >= 40:
            stop.set()  # budget exhausted: the domain message starved
        return Ack.ARCHIVE

    async def domain_handler(queue_name: str, message: QueueMessage) -> Ack:
        domain_consumed.set()
        stop.set()
        return Ack.ARCHIVE

    # The seed of the burst — the handler refills from here on.
    await queue.send(JOB)

    loop = EngineLoop(
        queues={INBOUND: queue, DOMAIN_EVENTS: domain_queue},
        handlers={INBOUND: sustained_inbound, DOMAIN_EVENTS: domain_handler},
        config=tiny_config,
        clock=SystemClock(),
        randomness=FixedRandomness(),
        stop=stop,
    )

    await asyncio.wait_for(loop.run(), 30)

    assert domain_consumed.is_set(), (
        f"the domain event starved behind {inbound_consumed} inbound consumes — "
        "beliefs must expire every window"
    )
    # And the bound is ONE window, not eventual: the reset fires at 15 consumed
    # turns, the domain slot comes right after.
    assert inbound_consumed <= 2 * 15

"""E0-08 · The runtime loop, reduced to what shutting down safely requires.

This is the skeleton, not the engine: no weighted polling, no backoff, no
per-tenant semaphore — those arrive in E1 from their own red tests. What is
specified here is the property that has to hold from the very first tick and
would be expensive to retrofit: **a shutdown never loses a job**. Either the
message was finished and archived, or it is still in the queue for the next
process to claim. Nothing in between.
"""

import asyncio

import pytest

from agents_runtime.clock import SystemClock
from agents_runtime.queueing.loop import RuntimeLoop
from agents_runtime.repository.queue import PgmqQueue, QueueMessage

JOB = {"conversation_id": "3f1c9a4b-0d2e-4f6a-9b8c-7d6e5f4a3b2c", "generation": 2, "target_seq": 11}

# Long enough that nothing in these tests can come back on its own, so a message
# that reappears is a bug and not a timeout.
VISIBILITY_TIMEOUT = 60

# The loop is not waiting for anything real here — the only reason it sleeps at
# all is to not spin on an empty queue.
IDLE_PAUSE = 0.01

# A hung loop must fail the suite instead of hanging CI.
DEADLINE = 5


def loop_for(queue: PgmqQueue, handle, stop: asyncio.Event) -> RuntimeLoop:
    return RuntimeLoop(
        queue=queue,
        handle=handle,
        clock=SystemClock(),
        stop=stop,
        visibility_timeout=VISIBILITY_TIMEOUT,
        idle_pause_seconds=IDLE_PAUSE,
    )


async def test_it_handles_a_queued_message_and_archives_it(queue: PgmqQueue, queue_length) -> None:
    stop = asyncio.Event()
    handled: list[QueueMessage] = []

    async def handle(message: QueueMessage) -> None:
        handled.append(message)
        stop.set()

    await queue.send(JOB)

    await asyncio.wait_for(loop_for(queue, handle, stop).run(), DEADLINE)

    assert [message.payload for message in handled] == [JOB]
    assert await queue_length() == 0, "a handled message ends archived, never in limbo"


async def test_it_claims_nothing_once_shutdown_was_requested(
    queue: PgmqQueue, queue_length
) -> None:
    stop = asyncio.Event()
    stop.set()
    handled: list[QueueMessage] = []

    async def handle(message: QueueMessage) -> None:
        handled.append(message)

    await queue.send(JOB)

    await asyncio.wait_for(loop_for(queue, handle, stop).run(), DEADLINE)

    assert handled == [], "draining starts by not claiming more work"
    assert await queue_length() == 1
    assert await queue.read(visibility_timeout=VISIBILITY_TIMEOUT) is not None, (
        "the job was left visible for the next process, not hidden by a claim"
    )


async def test_the_message_in_flight_is_finished_before_the_loop_exits(
    queue: PgmqQueue, queue_length
) -> None:
    stop = asyncio.Event()
    started = asyncio.Event()
    release = asyncio.Event()

    async def handle(message: QueueMessage) -> None:
        started.set()
        await release.wait()

    await queue.send(JOB)
    running = asyncio.create_task(loop_for(queue, handle, stop).run())

    await asyncio.wait_for(started.wait(), DEADLINE)
    stop.set()  # shutdown lands in the middle of the work
    release.set()
    await asyncio.wait_for(running, DEADLINE)

    assert await queue_length() == 0, "the in-flight job was finished, not abandoned"


async def test_a_failing_handler_leaves_the_message_in_the_queue(
    queue: PgmqQueue, queue_length
) -> None:
    stop = asyncio.Event()

    async def handle(message: QueueMessage) -> None:
        raise RuntimeError("the job blew up")

    await queue.send(JOB)

    # Failing loudly is the E0 behaviour on purpose: backoff, retry limits and
    # the DLQ are E1 (ADR-06). What must already be true is that the job is not
    # archived — losing it would be silent, and silence is what costs a sale.
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(loop_for(queue, handle, stop).run(), DEADLINE)

    assert await queue_length() == 1

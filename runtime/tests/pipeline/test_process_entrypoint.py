"""E0-08 · The process as the container starts it.

`python -m agents_runtime` is a shell around this: read the environment,
translate SIGTERM into "stop", call `run`. Everything below that shell is
exercised here — including the part no unit test can see, which is that the
process opens its own connection, does a tick of real work with it and returns
when asked to stop.
"""

import asyncio

from agents_runtime.app import run
from agents_runtime.repository.queue import PgmqQueue, QueueMessage

JOB = {"conversation_id": "8a7b6c5d-4e3f-4a2b-9c8d-7e6f5a4b3c2d", "generation": 3, "target_seq": 4}

DEADLINE = 5


async def test_the_process_consumes_one_tick_and_shuts_down(
    dsn: str, queue: PgmqQueue, queue_length
) -> None:
    stop = asyncio.Event()
    handled: list[dict] = []

    async def handle(message: QueueMessage) -> None:
        handled.append(message.payload)
        stop.set()

    await queue.send(JOB)

    await asyncio.wait_for(run(dsn, handle=handle, stop=stop, idle_pause_seconds=0.01), DEADLINE)

    assert handled == [JOB]
    assert await queue_length() == 0

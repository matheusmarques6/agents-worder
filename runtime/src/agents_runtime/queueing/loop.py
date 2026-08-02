"""The consumption loop — claim, work, finish, repeat, and drain on shutdown.

This is the E0 skeleton: one queue, one message at a time, no backoff and no
semaphore. Weighted polling (8:4:2:1 with age promotion), exponential backoff,
the DLQ and the per-tenant concurrency limit are E1 and arrive from their own
tests.

What is already load-bearing is the shape of the shutdown. Deploys are routine
(migrations → edge functions → runtime → hub), so "stop claiming, finish what
is in hand" is not an edge case: it runs on every release. A loop that checked
for shutdown in the middle of a job would have to decide what to do with a
half-finished one — so it does not. The only place it can stop is between jobs.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from agents_runtime.clock import Clock
from agents_runtime.repository.queue import QueueMessage

Handler = Callable[[QueueMessage], Awaitable[None]]


class Queue(Protocol):
    """What the loop needs from a queue — the transport itself lives in `repository`."""

    async def read(self, *, visibility_timeout: int) -> QueueMessage | None: ...

    async def archive(self, message_id: int) -> bool: ...


class RuntimeLoop:
    """Consumes one queue until asked to stop."""

    def __init__(
        self,
        *,
        queue: Queue,
        handle: Handler,
        clock: Clock,
        stop: asyncio.Event,
        visibility_timeout: int = 60,
        idle_pause_seconds: float = 1.0,
    ) -> None:
        self._queue = queue
        self._handle = handle
        self._clock = clock
        self._stop = stop
        self._visibility_timeout = visibility_timeout
        self._idle_pause_seconds = idle_pause_seconds

    async def run(self) -> None:
        while not self._stop.is_set():
            message = await self._queue.read(visibility_timeout=self._visibility_timeout)

            if message is None:
                await self._clock.sleep(self._idle_pause_seconds)
                continue

            # No shutdown check between these two lines: from the moment the
            # message is claimed, archiving it is the only way this loop is
            # allowed to let go of it.
            await self._handle(message)

            # The return value is dropped on purpose while there is exactly one
            # consumer: False can only mean "already archived", which nobody
            # else is in a position to have done. It stops being noise in E1 —
            # with job dedup and a second consumer, False is the evidence that
            # this loop worked on something someone else had already finished.
            await self._queue.archive(message.id)

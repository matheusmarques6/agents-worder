"""Composition root — where the process's parts are wired to each other.

Everything the runtime does is assembled here and nowhere else: the modules
themselves receive what they need (a queue, a clock, a stop signal) instead of
reaching for it. That is what keeps the loop testable against a real pgmq while
still being the same code the container runs.

E0 wires one queue and one handler. The four queues, the coalescer, the
scheduler and the senders join this same function in E1 as tasks of the single
asyncio process (ADR-02).
"""

import asyncio

import psycopg

from agents_runtime.clock import Clock, SystemClock
from agents_runtime.queueing import INBOUND
from agents_runtime.queueing.loop import Handler, RuntimeLoop
from agents_runtime.repository.queue import PgmqQueue

# Shows up in pg_stat_activity, so a connection left behind by the runtime has
# a name on it instead of being one more anonymous backend.
APPLICATION_NAME = "agents-runtime"


async def run(
    dsn: str,
    *,
    handle: Handler,
    stop: asyncio.Event,
    clock: Clock | None = None,
    idle_pause_seconds: float = 1.0,
) -> None:
    """Serve until `stop` is set, then return once the work in hand is finished."""
    async with await psycopg.AsyncConnection.connect(
        dsn, autocommit=True, application_name=APPLICATION_NAME
    ) as connection:
        loop = RuntimeLoop(
            queue=PgmqQueue(connection, INBOUND),
            handle=handle,
            clock=clock or SystemClock(),
            stop=stop,
            idle_pause_seconds=idle_pause_seconds,
        )
        await loop.run()

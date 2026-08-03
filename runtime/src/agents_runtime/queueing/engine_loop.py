"""The consumption loop — the unit-level polling policy, given hands.

`next_queue` (unidade 4) decides which queue gets the next turn; this loop
reads it, hands the message to that queue's handler, and finishes the message
the way the handler asked — or, when the handler blows up, the way the retry
rules decide. Every read ends in archive, set_vt or the DLQ: pgmq is never
left in limbo, and the E0-08 shutdown property still holds — the loop can
only stop BETWEEN jobs.

The loop keeps a belief about which queues have work, refreshed by what reads
actually return. An empty read marks the queue quiet — and the belief EXPIRES:
every full window of consumed turns, all beliefs reset to true. Without the
expiry, a sustained inbound burst would starve a queue once marked quiet for
the burst's whole duration (an order_paid waiting an hour to cancel a funnel —
the exact case ADR-5 exists to prevent). The cost of the expiry is at most one
empty read per queue per window; the guarantee is that no served queue goes
unprobed for longer than one window, which is the 8:4:2:1 promise kept under
load, not only at rest.
"""

import asyncio
import math
from collections.abc import Awaitable, Callable
from enum import Enum

from agents_runtime.clock import Clock
from agents_runtime.config import QueueingConfig
from agents_runtime.queueing.failures import classify
from agents_runtime.queueing.polling import next_queue
from agents_runtime.queueing.retries import DeadLetter, Retry, decide
from agents_runtime.randomness import Randomness
from agents_runtime.repository import engine
from agents_runtime.repository.queue import PgmqQueue, QueueMessage


class Ack(Enum):
    """How a handler wants its message finished."""

    ARCHIVE = "archive"
    RETRY_SHORT = "retry_short"


Handler = Callable[[str, QueueMessage], Awaitable[Ack]]


class EngineLoop:
    def __init__(
        self,
        *,
        queues: dict[str, PgmqQueue],
        handlers: dict[str, Handler],
        config: QueueingConfig,
        clock: Clock,
        randomness: Randomness,
        stop: asyncio.Event,
    ) -> None:
        self._queues = queues
        self._handlers = handlers
        self._config = config
        self._clock = clock
        self._randomness = randomness
        self._stop = stop
        # Served queues only: a queue without a handler is not polled, so a job
        # can never be read by a loop that has nothing to do with it.
        self._believed = dict.fromkeys(handlers, True)

    async def run(self) -> None:
        cursor = 0
        # Whole seconds, rounded up — pgmq truncates, and a truncated 0 means
        # "visible immediately", the opposite of a visibility timeout.
        vt = max(1, math.ceil(self._config.visibility_timeout.total_seconds()))
        window = sum(self._config.weights.values())
        consumed_since_reset = 0

        while not self._stop.is_set():
            queue_name, advanced = next_queue(self._believed, cursor, config=self._config)

            if queue_name is None:
                await self._rest()
                self._believed = dict.fromkeys(self._handlers, True)
                continue

            message = await self._queues[queue_name].read(visibility_timeout=vt)
            if message is None:
                self._believed[queue_name] = False
                continue

            cursor = advanced
            consumed_since_reset += 1
            if consumed_since_reset >= window:
                # Beliefs expire: one full window has been consumed, so every
                # served queue earns a fresh probe (the starvation fix).
                consumed_since_reset = 0
                self._believed = dict.fromkeys(self._handlers, True)
            # From here, finishing the message is the only way out — no
            # shutdown check until it is archived, delayed or dead-lettered.
            await self._dispatch(queue_name, message)

    async def _dispatch(self, queue_name: str, message: QueueMessage) -> None:
        queue = self._queues[queue_name]
        try:
            ack = await self._handlers[queue_name](queue_name, message)
        except Exception as error:  # the retry rules are the policy
            decision = decide(
                queue_name,
                attempt=message.read_count,
                failure=classify(error),
                config=self._config,
                randomness=self._randomness,
            )
            if isinstance(decision, Retry):
                await engine.set_visibility(
                    queue.connection, queue_name, message.id, decision.delay
                )
            elif isinstance(decision, DeadLetter):
                await engine.send_to_queue(
                    queue.connection,
                    decision.queue,
                    {**message.payload, "error_class": type(error).__name__,
                     "last_error": str(error)[:500]},
                )
                await queue.archive(message.id)
            return

        if ack is Ack.RETRY_SHORT:
            await engine.set_visibility(
                queue.connection, queue_name, message.id, self._config.busy_retry
            )
        else:
            await queue.archive(message.id)

    async def _rest(self) -> None:
        """Idle pause that a shutdown interrupts instead of waiting out."""
        sleep = asyncio.ensure_future(
            self._clock.sleep(self._config.idle_pause.total_seconds())
        )
        stopped = asyncio.ensure_future(self._stop.wait())
        _, pending = await asyncio.wait(
            {sleep, stopped}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()

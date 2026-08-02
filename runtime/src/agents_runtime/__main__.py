"""Process entrypoint — what `python -m agents_runtime` (the image's CMD) runs.

Deliberately thin: read the environment, turn SIGTERM into "stop", hand over to
`agents_runtime.app.run`. Everything worth testing lives below this line, in
the composition root, where a `pipeline` test can drive it against a real queue.

The E0 handler refuses the job instead of acknowledging it. A no-op that
archived whatever it read would be the one failure mode this milestone exists
to rule out — a job that disappears without anyone noticing. Until the workers
arrive in E1, the honest behaviour is to poll an empty queue quietly and die
loudly the moment something real shows up.
"""

import asyncio
import os
import signal

from agents_runtime.app import run
from agents_runtime.repository.queue import QueueMessage

DSN_VARIABLE = "SUPABASE_DB_URL"


async def _refuse(message: QueueMessage) -> None:
    raise NotImplementedError(
        f"agents-runtime: no worker exists yet for message {message.id}; "
        "it stays in the queue. Workers arrive in E1."
    )


def _stop_on_shutdown_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for received in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(received, stop.set)
        except NotImplementedError:  # Windows has no add_signal_handler
            signal.signal(received, lambda *_: stop.set())


async def _serve(dsn: str) -> None:
    stop = asyncio.Event()
    _stop_on_shutdown_signals(stop)
    await run(dsn, handle=_refuse, stop=stop)


def main() -> None:
    dsn = os.environ.get(DSN_VARIABLE)
    if not dsn:
        raise SystemExit(
            f"agents-runtime: {DSN_VARIABLE} is not set; there is nothing to connect to."
        )
    asyncio.run(_serve(dsn))


if __name__ == "__main__":
    main()

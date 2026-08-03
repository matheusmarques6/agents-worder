"""Process entrypoint — what `python -m agents_runtime` (the image's CMD) runs.

Deliberately thin: read the environment, turn SIGTERM into "stop", hand over
to `agents_runtime.app.run`. Everything worth testing lives below this line,
where the pipeline suite drives it against a real database.

The channel comes from `AGENTS_CHANNEL`, a `module:callable` factory that
receives the DSN. Unset means no sender task at all — explicit, instead of a
sender inventing outcomes against a channel that does not exist. The real
adapters land at the end of E1; the pipeline suite points this at its fake.
"""

import asyncio
import importlib
import os
import signal
import sys

from agents_runtime.app import run
from agents_runtime.channels.port import ChannelPort
from agents_runtime.config import config_from_env

DSN_VARIABLE = "SUPABASE_DB_URL"


def _channel_from_env(dsn: str) -> ChannelPort | None:
    spec = os.environ.get("AGENTS_CHANNEL")
    if not spec:
        return None
    module_name, _, attribute = spec.partition(":")
    factory = getattr(importlib.import_module(module_name), attribute)
    return factory(dsn)


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
    await run(
        dsn,
        stop=stop,
        config=config_from_env(dict(os.environ)),
        channel=_channel_from_env(dsn),
        process_name=os.environ.get("AGENTS_PROCESS_NAME", "agents-runtime"),
        worker_set_role=os.environ.get("AGENTS_WORKER_SET_ROLE"),
        sender_set_role=os.environ.get("AGENTS_SENDER_SET_ROLE"),
    )


def main() -> None:
    # psycopg's async connections need a selector loop; Windows defaults to
    # proactor (decisão 24, now at the entrypoint). Production is Linux, where
    # the selector already is the default and this line changes nothing.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    dsn = os.environ.get(DSN_VARIABLE)
    if not dsn:
        raise SystemExit(
            f"agents-runtime: {DSN_VARIABLE} is not set; there is nothing to connect to."
        )
    asyncio.run(_serve(dsn))


if __name__ == "__main__":
    main()

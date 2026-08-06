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
from agents_runtime.connectors.port import ConnectorPort

DSN_VARIABLE = "SUPABASE_DB_URL"

#: The responder factory. Required, unlike the channel — see below.
RESPONDER_VARIABLE = "AGENTS_RESPONDER"

#: The post-hoc reviewer factory (S9b). Required, like the responder: a process
#: that boots without it honours Judge 1 and still breaks RF-008 — the shadow
#: window promises the merchant that 100% of the replies are evaluated, and an
#: unset variable means `q_evals` is never polled. The queue-depth alert that
#: would make that noisy does not exist until E6, and the correction is the only
#: repair path for a violation that already reached a customer.
REVIEWER_VARIABLE = "AGENTS_REVIEWER"

_NO_RESPONDER = (
    "The process will not start without a real responder: falling back to the "
    "constant reply would answer customers without passing Judge 1, and "
    "CLAUDE.md allows no exception to that."
)

_NO_REVIEWER = (
    "The process will not start without a post-hoc reviewer: the shadow window "
    "of RF-008 promises the merchant 100% of replies evaluated, and an unset "
    "seam breaks that promise in silence — nothing polls q_evals, so no "
    "critical violation is ever repaired."
)


def _factory_from_env(variable: str, dsn: str, *, required: bool = False, refusal: str = ""):
    """module:callable, called with the DSN. Broken specs die at startup —
    absent and broken are different states (see tests/unit/test_channel_env).

    `required` exists because the seams fail in opposite directions. An absent
    CHANNEL means no sender task: nothing is sent, which is safe. An absent
    RESPONDER means `app.run` falls back to `fixed_responder`, and the constant
    reply of E1 is the one answer that reaches a customer without passing
    Judge 1. An absent REVIEWER audits nobody while the shadow window promises
    the merchant the opposite. So those two refuse to be absent, the way a
    channel refuses to be configured without a token (decisão 67): loud at
    startup, where a human is watching, instead of silently correct-looking in
    front of customers.

    `refusal` carries the promise each seam protects, because a 3am startup
    failure is only useful when it names what is broken rather than the variable
    that was missed.
    """
    spec = os.environ.get(variable)
    if not spec or not spec.strip():
        if required:
            raise RuntimeError(f"{variable} is not set. {refusal}".strip())
        return None
    module_name, _, attribute = spec.partition(":")
    factory = getattr(importlib.import_module(module_name), attribute)
    return factory(dsn)


def _channel_from_env(dsn: str) -> ChannelPort | None:
    return _factory_from_env("AGENTS_CHANNEL", dsn)


#: The connector seam (S8). `platform=module:callable` pairs, comma separated:
#:
#:     AGENTS_CONNECTORS=shopify=agents_runtime.connectors.shopify:from_env
#:
#: A map and not a single factory, because the reconciliation dispatches on
#: `connector_accounts.platform` — a deployment with Shopify and Nuvemshop
#: connected has two adapters and one sweep.
CONNECTORS_VARIABLE = "AGENTS_CONNECTORS"


def _connectors_from_env(dsn: str) -> dict[str, ConnectorPort]:
    """Unset means no reconciliation task at all — the channel's reading, not
    the responder's.

    Absent is SAFE here: without a sweep the webhook is still the primary path
    and nothing is invented; a sweep with no adapter would claim every store on
    every tick and close them all `error`, turning "not configured yet" into a
    hub full of red. Broken, though, is not absent: a spec that does not import
    dies at startup, where a human is watching.
    """
    spec = os.environ.get(CONNECTORS_VARIABLE, "")
    connectors: dict[str, ConnectorPort] = {}
    for entry in (part.strip() for part in spec.split(",")):
        if not entry:
            continue
        platform, separator, factory_spec = entry.partition("=")
        if not separator or not factory_spec.strip():
            raise RuntimeError(
                f"{CONNECTORS_VARIABLE} malformado em {entry!r}: "
                "esperado plataforma=modulo:callable"
            )
        module_name, _, attribute = factory_spec.strip().partition(":")
        connectors[platform.strip()] = getattr(
            importlib.import_module(module_name), attribute
        )(dsn)
    return connectors


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
        connectors=_connectors_from_env(dsn),
        # The responder seam, reachable from outside the process: cenário 4
        # holds a REAL subprocess inside FASE 2 through this.
        respond=_factory_from_env(RESPONDER_VARIABLE, dsn, required=True, refusal=_NO_RESPONDER),
        review=_factory_from_env(REVIEWER_VARIABLE, dsn, required=True, refusal=_NO_REVIEWER),
        process_name=os.environ.get("AGENTS_PROCESS_NAME", "agents-runtime"),
        worker_set_role=os.environ.get("AGENTS_WORKER_SET_ROLE"),
        sender_set_role=os.environ.get("AGENTS_SENDER_SET_ROLE"),
        reconcile_set_role=os.environ.get("AGENTS_RECONCILE_SET_ROLE"),
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

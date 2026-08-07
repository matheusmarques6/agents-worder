"""The W3C trace context as it travels in a queue payload.

`CLAUDE.md`, verbatim: "`traceparent` travels inside queue payloads (`otel`
column)". The slot has existed since E1 and all four job classes read it; what
never existed was a PRODUCER that wrote one. A slot that is only ever read is a
slot that will arrive empty on the day Logfire exists — and the day it arrives
empty, every span the runtime emits is a root, the coalescer tick and the turn
it caused look like two unrelated traces, and the exporter that cost the
credentials buys nothing.

**This module is the carrier and nothing else.** There is no SDK here, no
exporter, no OTLP endpoint and no sampler: those depend on Logfire and Grafana
Cloud (pendências B-2/B-3) and are deliberately not invented. What is here is
the shape the context takes in a payload, the validation that keeps a broken one
out, and the one function a call site names when it has no context to give.

**Fail closed, like everything else in this codebase.** A malformed traceparent
is dropped rather than forwarded. A trace that is absent is a fact an operator
reads correctly ("nothing instrumented this yet"); a trace that is wrong splits
one operation into two in the backend and nothing anywhere says so.
"""

from collections.abc import Callable
from typing import Any

#: The W3C header names, as they travel inside the `otel` slot. Held as literals
#: rather than imported from an SDK: the SDK is not a dependency yet, and the
#: wire format is a spec, not a library detail.
TRACEPARENT = "traceparent"
TRACESTATE = "tracestate"

#: What a job's `otel` slot holds. A dict of header name → value, which is the
#: shape every OpenTelemetry propagator injects into and extracts from — so the
#: day the SDK arrives, `TraceContextTextMapPropagator().extract(job.otel)` is
#: the whole integration.
Carrier = dict[str, str]

#: Where a producer gets the context it stamps. A callable, so the composition
#: root can hand the real thing in later without any call site changing shape.
TraceSource = Callable[[], Carrier | None]

_HEX = "0123456789abcdef"

#: Version `ff` is reserved by the spec and must never be sent.
_RESERVED_VERSION = "ff"


def no_trace_context() -> None:
    """The default until B-2/B-3: nothing is instrumented, so nothing is claimed.

    `None`, never `{}`. An empty dict in a payload would say "there was a trace
    and it was empty", which is a different (and false) statement from "nobody
    has instrumented this yet".
    """
    return None


def carrier(traceparent: str, *, tracestate: str | None = None) -> Carrier | None:
    """A validated `traceparent` (and its optional `tracestate`) as a carrier.

    Returns `None` when the header is not a traceparent this process would be
    willing to forward. The validation is the spec's own: four dash-separated
    fields, a version that is not the reserved `ff`, a 32-hex trace id and a
    16-hex span id, neither of which may be all zeros.
    """
    if not _is_valid_traceparent(traceparent):
        return None

    context: Carrier = {TRACEPARENT: traceparent}
    if tracestate:
        context[TRACESTATE] = tracestate
    return context


def _is_valid_traceparent(value: str) -> bool:
    parts = value.split("-")
    if len(parts) != 4:
        return False

    version, trace_id, span_id, flags = parts
    return (
        _is_hex(version, 2)
        and version != _RESERVED_VERSION
        and _is_hex(trace_id, 32)
        and set(trace_id) != {"0"}
        and _is_hex(span_id, 16)
        and set(span_id) != {"0"}
        and _is_hex(flags, 2)
    )


def _is_hex(value: str, length: int) -> bool:
    return len(value) == length and all(char in _HEX for char in value)


def stamp(payload: dict[str, Any], context: Carrier | None) -> dict[str, Any]:
    """The payload with its `otel` slot filled — or exactly the payload it was.

    The key is absent when there is no context, and that is load-bearing rather
    than tidy: the S4 lock (`test_no_fact_travels_in_the_payload`) says only ids
    travel on `q_scheduled`, and a fixed `"otel": null` would satisfy it while
    littering the queue with a key that asserts nothing.
    """
    if not context:
        return payload
    return {**payload, "otel": dict(context)}

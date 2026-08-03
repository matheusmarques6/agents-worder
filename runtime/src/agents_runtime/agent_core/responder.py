"""The E1 responder: a fixed reply.

Deliberately constant. A concurrency bug and a bad model behaviour produce the
same symptom — "the reply came out wrong" — and separating the two is the only
way to debug either. While the reply is constant, every observed difference is
the engine's. The real agent (prompt layers, tools, Judge) is E2, and it will
implement this same callable shape.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from agents_runtime.queueing.jobs import InboundJob

Responder = Callable[[InboundJob], Awaitable[dict[str, Any]]]

FIXED_REPLY = "Recebemos sua mensagem! Já estamos cuidando do seu pedido. 🧡"


def fixed_responder(text: str = FIXED_REPLY) -> Responder:
    async def respond(job: InboundJob) -> dict[str, Any]:
        return {"text": text}

    return respond

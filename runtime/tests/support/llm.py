"""Stand-ins for the model ports.

The embedder is the one a tool needs, and it is deliberately more than a stub:
it embeds with the deterministic embedder (so retrieval is really exercised),
it can watch the connection it is called with (so "no transaction spans a
provider call" is an assertion instead of a hope), it can move an injected
clock (so latency is a number), and it can fail on demand.

Lives under `tests/` because a double never ships in the runtime image
(decisão 8).
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import psycopg

from agents_runtime.agent_core.llm import EmbeddingResult, Usage
from tests.support.clock import FrozenClock
from tests.support.embedding import embed_text

START = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)

#: What the stand-in "costs" in clock time — 40ms, so a latency assertion has a
#: number to be equal to instead of a range to be inside of.
TAKES = timedelta(milliseconds=40)


class EmbeddingStandIn:
    def __init__(
        self,
        *,
        clock: FrozenClock | None = None,
        watching: psycopg.AsyncConnection | None = None,
        seen: list | None = None,
        fails: BaseException | None = None,
    ) -> None:
        self._clock = clock
        self._watching = watching
        self._seen = seen
        self._fails = fails

    async def embed(self, texts: Sequence[str], *, model: str) -> EmbeddingResult:
        if self._watching is not None and self._seen is not None:
            # The whole point: what the connection was doing at the exact moment
            # a provider call happened.
            self._seen.append(self._watching.info.transaction_status)
        if self._clock is not None:
            self._clock.advance(TAKES)
        if self._fails is not None:
            raise self._fails

        return EmbeddingResult(
            vectors=tuple(embed_text(text) for text in texts),
            usage=Usage(input_tokens=len(texts), output_tokens=None, cost_usd=0.0),
            model=model,
        )

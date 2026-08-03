"""The meter — every completed LLM call leaves a row of cost, and nothing else.

It wraps the port instead of living inside the adapter so the trail survives a
second adapter: whatever speaks to a model is wrapped here, and
`internal.llm_calls` is written by one code path for the agent, the judge and
the embeddings.

Three rules, each of them a decision of S5:

  * **Latency comes from the injected clock.** The fitness function of E0-06
    forbids reading the wall clock outside `clock.py`, and a clock a test can
    move is what makes latency an assertion instead of a range.
  * **A failed call leaves no row.** `llm_calls` (§6.4) has no error column: a
    row for a call that produced nothing would be a cost that never existed.
    Failures belong to the outbox and to `alerts`, not to the invoice.
  * **Content never enters a record.** Prompt, reply and phone number stay in
    Postgres; what leaves for the telemetry of S10 is cost, latency and ids.

Who writes the row is the caller's business: `record` is a port, and the
short transaction with `SET LOCAL app.tenant_id` around
`repository.llm_calls.record_llm_call` is opened where a connection exists.
Opening one here would mean a transaction living across an LLM call, which is
exactly what ADR-6 forbids.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from agents_runtime.agent_core.llm import ChatRequest, ChatResult, EmbeddingResult, LlmPort
from agents_runtime.clock import Clock

#: An embedding is an embedding whatever the meter was built for — the purpose
#: of a chat call is the composition's choice, this one is not.
EMBEDDING_PURPOSE = "embedding"


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One line of the invoice. Every field is a number or an identifier."""

    purpose: str
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    latency_ms: int


class Recorder(Protocol):
    async def __call__(self, record: CallRecord) -> None: ...


class MeteredLlm:
    """An `LlmPort` that bills. `purpose` separates the agent's spend from the
    gate's on the cost screen, so it is fixed when the meter is built."""

    def __init__(self, inner: LlmPort, *, clock: Clock, record: Recorder, purpose: str) -> None:
        self._inner = inner
        self._clock = clock
        self._record = record
        self._purpose = purpose

    async def chat(self, request: ChatRequest) -> ChatResult:
        started = self._clock.now()
        result = await self._inner.chat(request)
        await self._bill(self._purpose, result.provider, result.model, result, started)
        return result

    async def embed(self, texts: Sequence[str], *, model: str) -> EmbeddingResult:
        started = self._clock.now()
        result = await self._inner.embed(texts, model=model)
        await self._bill(EMBEDDING_PURPOSE, result.provider, result.model, result, started)
        return result

    async def _bill(
        self,
        purpose: str,
        provider: str,
        model: str,
        result: ChatResult | EmbeddingResult,
        started: datetime,
    ) -> None:
        await self._record(
            CallRecord(
                purpose=purpose,
                provider=provider,
                model=model,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                cost_usd=result.usage.cost_usd,
                latency_ms=_elapsed_ms(started, self._clock.now()),
            )
        )


def _elapsed_ms(started: datetime, finished: datetime) -> int:
    return int((finished - started).total_seconds() * 1000)

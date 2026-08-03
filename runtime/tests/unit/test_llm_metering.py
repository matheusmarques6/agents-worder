"""Every LLM call leaves a trail — tokens, cost and latency, per call.

The meter wraps the port instead of living inside the adapter for one reason:
the trail has to survive a second adapter. Whatever speaks to a model goes
through this wrapper, and `internal.llm_calls` is written by the same code path
for chat, for the judge and for embeddings.

Latency is measured with the injected `Clock` — the fitness function of E0-06
forbids reading the wall clock here, and a `FrozenClock` the stand-in advances
is what makes the assertion a number instead of a range.

PII law, explicit (S5 of the plano E2): what is metered is COST, never content.
No prompt, no reply and no phone number reaches a `CallRecord` — the content
stays in Postgres, and the telemetry of S10 only ever sees these fields.
"""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest

from agents_runtime.agent_core.llm import ChatRequest, ChatResult, EmbeddingResult, Message, Usage
from agents_runtime.agent_core.metering import CallRecord, MeteredLlm
from tests.support.clock import FrozenClock

START = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


class SlowStandIn:
    """An LLM that takes exactly `takes` of the injected clock's time."""

    def __init__(self, clock: FrozenClock, *, takes=timedelta(milliseconds=900), fails=None):
        self._clock = clock
        self._takes = takes
        self._fails = fails

    async def chat(self, request: ChatRequest):
        self._clock.advance(self._takes)
        if self._fails is not None:
            raise self._fails
        return _a_result(request)

    async def embed(self, texts, *, model: str) -> EmbeddingResult:
        self._clock.advance(self._takes)
        if self._fails is not None:
            raise self._fails
        return EmbeddingResult(
            vectors=tuple((0.1,) for _ in texts),
            usage=Usage(input_tokens=8, output_tokens=None, cost_usd=0.000002),
            model=f"openai/{model}",
        )


def _a_result(request: ChatRequest) -> ChatResult:
    return ChatResult(
        text="claro, já verifico!",
        usage=Usage(input_tokens=120, output_tokens=40, cost_usd=0.00018),
        model=f"anthropic/{request.model}",
    )


class Ledger:
    """A recorder that keeps what it was told, instead of a database."""

    def __init__(self) -> None:
        self.records: list[CallRecord] = []

    async def __call__(self, record: CallRecord) -> None:
        self.records.append(record)


def a_request(**overrides) -> ChatRequest:
    defaults = dict(model="claude-sonnet-5", messages=(Message(role="user", content="oi"),))
    return ChatRequest(**{**defaults, **overrides})


def metered(clock: FrozenClock, ledger: Ledger, *, purpose="agent_reply", fails=None) -> MeteredLlm:
    return MeteredLlm(SlowStandIn(clock, fails=fails), clock=clock, record=ledger, purpose=purpose)


class TestTheTrail:
    async def test_a_chat_call_records_tokens_cost_and_latency(self) -> None:
        clock, ledger = FrozenClock(START), Ledger()

        await metered(clock, ledger).chat(a_request())

        (record,) = ledger.records
        assert record.purpose == "agent_reply"
        assert record.provider == "openrouter"
        assert record.input_tokens == 120
        assert record.output_tokens == 40
        assert record.cost_usd == pytest.approx(0.00018)
        assert record.latency_ms == 900

    async def test_the_model_recorded_is_the_one_that_was_billed(self) -> None:
        """The caller asks for `claude-sonnet-5`; OpenRouter bills a fully
        qualified name. The invoice is what the cost trail has to match."""
        clock, ledger = FrozenClock(START), Ledger()

        await metered(clock, ledger).chat(a_request(model="claude-sonnet-5"))

        assert ledger.records[0].model == "anthropic/claude-sonnet-5"

    async def test_the_purpose_of_a_judge_call_is_its_own(self) -> None:
        """`purpose` is what separates the agent's spend from the gate's on the
        cost screen — one meter per purpose, decided at composition."""
        clock, ledger = FrozenClock(START), Ledger()

        await metered(clock, ledger, purpose="judge_pre").chat(a_request())

        assert ledger.records[0].purpose == "judge_pre"

    async def test_an_embedding_is_always_recorded_as_an_embedding(self) -> None:
        clock, ledger = FrozenClock(START), Ledger()

        await metered(clock, ledger, purpose="agent_reply").embed(["oi"], model="e3-small")

        assert ledger.records[0].purpose == "embedding"

    async def test_the_result_passes_through_untouched(self) -> None:
        clock, ledger = FrozenClock(START), Ledger()

        result = await metered(clock, ledger).chat(a_request())

        assert result.text == "claro, já verifico!"
        assert result.usage.input_tokens == 120


class TestWhatIsNotRecorded:
    async def test_a_failed_call_leaves_no_cost_row(self) -> None:
        """`llm_calls` has no error column (S2) — a row for a call that produced
        nothing would be a cost that never existed. Failures are the outbox's
        and the alert's business, not the invoice's."""
        clock, ledger = FrozenClock(START), Ledger()

        with pytest.raises(RuntimeError):
            await metered(clock, ledger, fails=RuntimeError("HTTP 500 boom")).chat(a_request())

        assert ledger.records == []

    async def test_no_prompt_or_reply_reaches_the_record(self) -> None:
        """The PII law of the milestone, as an assertion: content lives in
        Postgres and never leaves it as telemetry."""
        clock, ledger = FrozenClock(START), Ledger()

        await metered(clock, ledger).chat(
            a_request(messages=(Message(role="user", content="meu CPF é 000.000.000-00"),))
        )

        recorded = asdict(ledger.records[0])
        # Neither what went in nor what came out: the whole record, field by
        # field, is numbers and identifiers.
        assert not any("CPF" in value for value in recorded.values() if isinstance(value, str))
        assert not any("verific" in value for value in recorded.values() if isinstance(value, str))

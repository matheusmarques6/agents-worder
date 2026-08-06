"""A model that asks for tools, on a script.

There is no LLM key here and there never was (the whole E2 closed against
injectable doubles), so the loop is proven against a model whose every turn is
written down: either a list of tool calls to demand, or the text to answer with.

The double records the REQUESTS, not just the answers, because half of what the
loop has to be right about is what it offers — a ceiling that stops asking but
keeps offering tools is a ceiling that has not been reached.

Lives under `tests/` because a double never ships in the runtime image
(decisão 8).
"""

import itertools
from collections.abc import Sequence

from agents_runtime.agent_core.llm import ChatRequest, ChatResult, EmbeddingResult, ToolCall, Usage
from agents_runtime.judges.pre_send import JUDGE_MODEL
from tests.support.llm import EmbeddingStandIn, ScriptedLlm


class Demand:
    """One scripted turn in which the model asks for tools instead of answering."""

    def __init__(self, *calls: tuple[str, str]) -> None:
        #: (tool name, raw argument text) — raw, because the wire carries text
        #: and "what happens when it is not JSON" is a rule under test.
        self.calls = calls


class ToolCallingLlm:
    """`script` is read one entry per turn. A `Demand` asks for tools; a `str`
    answers. When the script runs out, the model repeats its last entry — which
    is how "a model that never stops asking" is expressed without writing an
    infinite list."""

    def __init__(self, script: Sequence[Demand | str]) -> None:
        if not script:
            raise ValueError("a scripted model with no script proves nothing")
        self._script = list(script)
        self.asked: list[ChatRequest] = []
        self._ids = itertools.count(1)
        #: Judge 1 is a different model on the same port, exactly as in
        #: production. Its turns are delegated and NOT counted in `asked`: a
        #: ceiling assertion counting judge calls would count the wrong thing.
        self._judge = ScriptedLlm()

    async def chat(self, request: ChatRequest) -> ChatResult:
        if request.model == JUDGE_MODEL:
            return await self._judge.chat(request)

        turn = self._script[min(len(self.asked), len(self._script) - 1)]
        self.asked.append(request)

        if isinstance(turn, str):
            return self._result(turn, ())

        # A model cannot call a tool it was not offered — the double refuses to
        # pretend otherwise, because a loop that stopped offering tools and was
        # still asked for one would "pass" a ceiling test it had failed.
        if not request.tools:
            return self._result("Pelo que vi, é isso. 🧡", ())

        return self._result(
            "",
            tuple(
                ToolCall(id=f"call-{next(self._ids)}", name=name, arguments=arguments)
                for name, arguments in turn.calls
            ),
        )

    def _result(self, text: str, calls: tuple[ToolCall, ...]) -> ChatResult:
        return ChatResult(
            text=text,
            usage=Usage(input_tokens=100, output_tokens=20, cost_usd=0.0001),
            model="stand-in/tool-calling",
            tool_calls=calls,
        )

    async def embed(self, texts: Sequence[str], *, model: str) -> EmbeddingResult:
        return await EmbeddingStandIn().embed(texts, model=model)

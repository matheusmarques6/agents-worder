"""The loop where the model chooses — with a ceiling, because it is a loop.

The E2 had no loop, and said so: its two tools were unconditional context
because there was no choice to make, and a round trip costs seconds in a
WhatsApp conversation. The E3 creates the choice — is this about an order, is it
about a parcel, is this person asking for a human — so the model has to be able
to ask, read the answer and ask again.

What that adds to the product is a way for one turn to never end. A model that
keeps requesting tools keeps being obeyed; nothing about a conversation makes it
stop. So the ceiling is not a safety net bolted on afterwards, it is the shape
of the loop:

  * the model may ASK for tools `max_rounds` times;
  * when it exhausts them, it gets ONE more turn with **no tools offered at
    all**, and a model with nothing to call has nothing to do but answer.

That last turn is what makes the ceiling terminate rather than merely cut off.
Stopping at the limit and returning whatever text happened to be lying around
would hand the customer either silence or a half sentence; taking the tools away
and asking again hands them an answer built on what was actually learned.

Two things the model asks for are refused without ever being executed, and both
are refused the same way — as a failed tool answer it can read and correct,
never as an exception that costs the customer their reply:

  * **a name outside this agent's set.** The registry decides what a tenant's
    agent holds (`arquitetura §3`); a name that is not in the toolset handed to
    this loop is not looked up anywhere else. A message from a contact never
    enables a tool outside the tenant's set, and that is enforced by the lookup
    being the toolset itself, not by the prompt having listed the right names;
  * **arguments that are not JSON.** Never "use what parsed", never an empty
    mapping standing in for text nobody could read — an empty mapping would run
    the tool with its defaults, which for `get_order` means answering about a
    different order than the one asked about.

Neither refusal writes a row in `internal.tool_calls`, and that is honest rather
than lazy: that table records EXECUTIONS, and nothing executed. What the loop
does instead is return them, so a caller that wants to count them can.

No clock, no I/O, no database: the loop takes the runner as a callable, so every
rule here is testable without a connection and without a provider.
"""

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agents_runtime.agent_core.llm import ChatRequest, LlmPort, Message, ToolCall
from agents_runtime.tools.base import Tool, ToolResult

#: How many times the model may ask for tools before it is asked to answer.
#: Three covers the deepest real chain the E3 has — "which order? / where is it?
#: / this needs a person" — and a fourth has never been the difference between a
#: good reply and a bad one, only between one reply and two provider bills.
MAX_TOOL_ROUNDS = 3

UNKNOWN_TOOL = "tool not available to this agent"
BAD_ARGUMENTS = "arguments are not valid JSON"


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    text: str
    #: How many times the model asked for tools.
    rounds: int
    #: True when it used them all and was made to answer without tools.
    ceiling_reached: bool
    #: Names actually run, in order — what `internal.tool_calls` will hold.
    executed: tuple[str, ...]
    #: Names asked for and never run. Nothing executed, so nothing is recorded;
    #: returning them is what keeps that from being silent.
    refused: tuple[str, ...]


#: How the loop reaches a tool. `run_tool` bound to a connection, a context and
#: a clock by the caller — the loop itself never learns what any of those are.
ToolRunner = Callable[[Tool, Mapping[str, Any]], Awaitable[ToolResult]]


async def converse(
    chat: LlmPort,
    *,
    model: str,
    messages: Sequence[Message],
    toolset: Mapping[str, Tool],
    run: ToolRunner,
    think: bool = False,
    max_rounds: int = MAX_TOOL_ROUNDS,
) -> LoopOutcome:
    offered = tuple(tool.spec for tool in toolset.values())
    history = list(messages)
    executed: list[str] = []
    refused: list[str] = []

    for round_index in range(max_rounds):
        answer = await chat.chat(
            ChatRequest(model=model, messages=tuple(history), think=think, tools=offered)
        )
        if not answer.tool_calls:
            return LoopOutcome(
                text=answer.text,
                rounds=round_index,
                ceiling_reached=False,
                executed=tuple(executed),
                refused=tuple(refused),
            )

        # The provider will not read the answers without its own request echoed
        # back. `content` is whatever the model said alongside the request —
        # usually nothing.
        history.append(
            Message(role="assistant", content=answer.text, tool_calls=answer.tool_calls)
        )

        for call in answer.tool_calls:
            tool = toolset.get(call.name)
            arguments = _parse(call)

            if tool is None:
                refused.append(call.name)
                result = ToolResult(tool=call.name, success=False, error=UNKNOWN_TOOL)
            elif arguments is None:
                refused.append(call.name)
                result = ToolResult(tool=call.name, success=False, error=BAD_ARGUMENTS)
            else:
                executed.append(call.name)
                result = await run(tool, arguments)

            history.append(
                Message(
                    role="tool",
                    content=_answer_for(result),
                    tool_call_id=call.id,
                )
            )

    # The ceiling. NOT a cut-off: the tools are taken away and the model is
    # asked once more, because a model with nothing to call has nothing to do
    # but answer — and the customer is owed an answer, not a half sentence.
    final = await chat.chat(
        ChatRequest(model=model, messages=tuple(history), think=think)
    )
    return LoopOutcome(
        text=final.text,
        rounds=max_rounds,
        ceiling_reached=True,
        executed=tuple(executed),
        refused=tuple(refused),
    )


def _answer_for(result: ToolResult) -> str:
    """What the model reads back. JSON because it is what the model asked in,
    and `ensure_ascii=False` because a parcel status in Portuguese should not
    reach the prompt as escape sequences."""
    return json.dumps(
        {"success": result.success, "output": result.output, "error": result.error},
        ensure_ascii=False,
    )


def _parse(call: ToolCall) -> Mapping[str, Any] | None:
    try:
        parsed = json.loads(call.arguments or "{}")
    except ValueError:
        return None
    # A JSON scalar or list is not an arguments object. Accepting one would mean
    # deciding which parameter it was meant to be.
    return parsed if isinstance(parsed, dict) else None

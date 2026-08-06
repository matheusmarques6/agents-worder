"""O laço em que o modelo escolhe — e o teto, que é a forma dele.

The E2 had no loop and said why: two tools, no choice, and a round trip costs
seconds in a WhatsApp conversation. The E3 creates the choice (which order,
where is the parcel, does this need a person), and with it the one failure mode
a turn never had — a model that keeps asking for tools keeps being obeyed, and
nothing about a conversation makes it stop.

So the claims here are, in order of how much they cost when wrong:

  * the turn always ends, and it ends with TEXT. Not with silence, not with a
    half sentence: at the ceiling the tools are taken away and the model is
    asked once more, because a model with nothing to call has nothing to do but
    answer;
  * a name outside this agent's toolset is never executed. The registry is what
    decides what a tenant's agent holds, and a contact's message may not add to
    it — enforced by the lookup being the toolset itself, never by the prompt
    having listed the right names;
  * arguments that are not JSON are refused, never guessed. An empty mapping
    standing in for unreadable text would run `get_order` with its defaults,
    which answers about a DIFFERENT order than the one asked about.

No connection and no provider: the runner is a callable, so every rule is a
unit.
"""

import json

import pytest

from agents_runtime.agent_core.llm import Message, ToolSpec
from agents_runtime.agent_core.tool_loop import (
    BAD_ARGUMENTS,
    MAX_TOOL_ROUNDS,
    UNKNOWN_TOOL,
    converse,
)
from agents_runtime.tools.base import ToolResult
from tests.support.tool_calling import Demand, ToolCallingLlm

MODEL = "claude-sonnet-5"
A_QUESTION = (Message(role="user", content="cadê meu pedido?"),)


class SpyTool:
    """A tool that records what it was asked and answers what it was told to."""

    def __init__(self, name: str, output: dict | None = None, *, success: bool = True) -> None:
        self.name = name
        self.spec = ToolSpec(name=name, description=f"a tool called {name}", parameters={})
        self._output = output or {}
        self._success = success
        self.calls: list[dict] = []

    async def __call__(self, conn, context, arguments) -> ToolResult:  # pragma: no cover
        raise AssertionError("the loop must reach a tool through the runner, never directly")


def a_runner(seen: list[tuple[str, dict]]):
    """Stands where `run_tool` stands — bound to a connection, a context and a
    clock the loop never learns about."""

    async def run(tool: SpyTool, arguments: dict) -> ToolResult:
        seen.append((tool.name, dict(arguments)))
        tool.calls.append(dict(arguments))
        return ToolResult(tool=tool.name, success=tool._success, output=tool._output)

    return run


async def loop(script, toolset, seen=None, **overrides):
    llm = ToolCallingLlm(script)
    outcome = await converse(
        llm,
        model=MODEL,
        messages=A_QUESTION,
        toolset={tool.name: tool for tool in toolset},
        run=a_runner(seen if seen is not None else []),
        **overrides,
    )
    return llm, outcome


class TestTheChoice:
    async def test_the_model_is_offered_exactly_the_tools_this_agent_holds(self) -> None:
        """`arquitetura §3`: the registry decides the subset, and the offer is
        that subset — not a catalogue the prompt asked it to be polite about."""
        order, tracking = SpyTool("get_order"), SpyTool("get_tracking")

        llm, _ = await loop(["Seu pedido saiu para entrega 🧡"], (order, tracking))

        assert [spec.name for spec in llm.asked[0].tools] == ["get_order", "get_tracking"]

    async def test_a_tool_the_model_asked_for_is_run_and_its_answer_goes_back(self) -> None:
        order = SpyTool("get_order", {"found": True, "order": {"order_id": "1001"}})
        seen: list = []

        llm, outcome = await loop(
            [Demand(("get_order", '{"order_id": "1001"}')), "Seu pedido 1001 está pago 🧡"],
            (order,),
            seen,
        )

        assert seen == [("get_order", {"order_id": "1001"})]
        assert outcome.text == "Seu pedido 1001 está pago 🧡"
        assert outcome.executed == ("get_order",)
        assert outcome.rounds == 1
        assert outcome.ceiling_reached is False

        # The second call carries the conversation so far: the model's own
        # request echoed back, then the answer to it, tied by the call id.
        second = llm.asked[1].messages
        assert second[-2].role == "assistant"
        assert second[-2].tool_calls[0].name == "get_order"
        assert second[-1].role == "tool"
        assert second[-1].tool_call_id == second[-2].tool_calls[0].id
        assert json.loads(second[-1].content)["output"]["order"]["order_id"] == "1001"

    async def test_two_tools_in_one_turn_are_both_run_and_each_answered_by_its_own_id(
        self,
    ) -> None:
        """Providers may ask for several at once, and pairing an answer with the
        wrong request is a bug that only ever looks like a confused agent."""
        order, tracking = SpyTool("get_order", {"found": True}), SpyTool("get_tracking")
        seen: list = []

        llm, outcome = await loop(
            [Demand(("get_order", "{}"), ("get_tracking", "{}")), "Prontinho 🧡"],
            (order, tracking),
            seen,
        )

        assert [name for name, _ in seen] == ["get_order", "get_tracking"]
        assert outcome.executed == ("get_order", "get_tracking")

        asked_for = llm.asked[1].messages[-3].tool_calls
        answers = llm.asked[1].messages[-2:]
        assert [answer.tool_call_id for answer in answers] == [call.id for call in asked_for]

    async def test_a_model_that_never_asks_costs_exactly_one_call(self) -> None:
        """The E2 behaviour, unchanged: nothing about the loop makes a simple
        turn more expensive than it was."""
        llm, outcome = await loop(["Oi! Como posso ajudar? 🧡"], (SpyTool("get_order"),))

        assert len(llm.asked) == 1
        assert outcome.rounds == 0
        assert outcome.executed == ()

    async def test_an_agent_with_no_tools_is_offered_none(self) -> None:
        """A tenant that enabled nothing is a legitimate configuration — an agent
        that only talks. It must not be handed a toolset because the loop
        exists."""
        llm, outcome = await loop(["Oi! 🧡"], ())

        assert llm.asked[0].tools == ()
        assert outcome.text == "Oi! 🧡"


class TestTheCeiling:
    async def test_the_customer_gets_an_answer_even_when_the_model_never_stops_asking(
        self,
    ) -> None:
        """The failure mode the loop invented, and the reason the ceiling is not
        optional. The model here asks forever; the turn still ends, in text."""
        order = SpyTool("get_order")
        seen: list = []

        llm, outcome = await loop([Demand(("get_order", "{}"))], (order,), seen)

        assert outcome.ceiling_reached is True
        assert outcome.rounds == MAX_TOOL_ROUNDS
        assert len(seen) == MAX_TOOL_ROUNDS
        # `max_rounds` chances to ask, then one turn to answer. Never more.
        assert len(llm.asked) == MAX_TOOL_ROUNDS + 1
        assert outcome.text != ""

    async def test_the_last_turn_offers_no_tools_at_all(self) -> None:
        """What makes the ceiling terminate rather than merely cut off. Stopping
        at the limit and returning whatever text was lying around would hand the
        customer a half sentence; taking the tools away and asking again hands
        them an answer built on what was actually learned."""
        llm, _ = await loop([Demand(("get_order", "{}"))], (SpyTool("get_order"),))

        assert [bool(request.tools) for request in llm.asked] == [
            *[True] * MAX_TOOL_ROUNDS,
            False,
        ]

    async def test_the_ceiling_is_a_number_the_caller_can_state(self) -> None:
        llm, outcome = await loop(
            [Demand(("get_order", "{}"))], (SpyTool("get_order"),), max_rounds=1
        )

        assert outcome.rounds == 1
        assert len(llm.asked) == 2


class TestWhatIsRefusedWithoutRunning:
    async def test_a_tool_outside_this_agents_set_is_never_run(self) -> None:
        """A contact's message never enables a tool outside the tenant's set. The
        toolset IS the lookup — there is no second place to find a name in."""
        order = SpyTool("get_order")
        seen: list = []

        llm, outcome = await loop(
            [Demand(("cancel_order", '{"order_id": "1001"}')), "Não consigo cancelar por aqui."],
            (order,),
            seen,
        )

        assert seen == []
        assert outcome.executed == ()
        assert outcome.refused == ("cancel_order",)

        answer = json.loads(llm.asked[1].messages[-1].content)
        assert answer["success"] is False
        assert answer["error"] == UNKNOWN_TOOL

    async def test_arguments_that_are_not_json_are_refused_not_guessed(self) -> None:
        """Never "use what parsed". An empty mapping standing in for unreadable
        text would run `get_order` with its defaults — which answers about a
        different order than the one asked about."""
        order = SpyTool("get_order")
        seen: list = []

        llm, outcome = await loop(
            [Demand(("get_order", "{pedido: 1001")), "Pode repetir o número? 🧡"],
            (order,),
            seen,
        )

        assert seen == []
        assert outcome.refused == ("get_order",)
        assert json.loads(llm.asked[1].messages[-1].content)["error"] == BAD_ARGUMENTS

    @pytest.mark.parametrize("arguments", ['"1001"', "[1001]", "null"])
    async def test_arguments_that_are_json_but_not_an_object_are_refused(
        self, arguments: str
    ) -> None:
        """Accepting one would mean deciding which parameter it was meant to be."""
        seen: list = []

        _, outcome = await loop(
            [Demand(("get_order", arguments)), "..."], (SpyTool("get_order"),), seen
        )

        assert seen == []
        assert outcome.refused == ("get_order",)

    async def test_an_empty_argument_object_is_a_legitimate_call(self) -> None:
        """`get_order` with no arguments means "the most recent one" — the
        commonest call there is. Refusing it would refuse the default."""
        seen: list = []

        _, outcome = await loop(
            [Demand(("get_order", "{}")), "Achei aqui 🧡"], (SpyTool("get_order"),), seen
        )

        assert seen == [("get_order", {})]
        assert outcome.executed == ("get_order",)


class TestFailure:
    async def test_a_tool_that_failed_is_an_answer_the_model_works_without(self) -> None:
        """Never an exception that costs the customer their reply: the failure is
        already a row in `internal.tool_calls`, and the model is told."""
        order = SpyTool("get_order", success=False)

        llm, outcome = await loop(
            [Demand(("get_order", "{}")), "Não consegui consultar agora, me dá um minuto? 🧡"],
            (order,),
        )

        assert outcome.text.startswith("Não consegui consultar")
        assert outcome.executed == ("get_order",)
        assert json.loads(llm.asked[1].messages[-1].content)["success"] is False

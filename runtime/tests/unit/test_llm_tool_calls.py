"""Tool calling on the wire — the OpenAI-compatible half the E3 needed.

`httpx.MockTransport` keeps everything in-process, so this is honestly `unit`.
What it CANNOT prove is that OpenRouter really returns `tool_calls` in this
shape for the tenant's model — that is the `contract` suite, on demand, with a
real key.

A separate file from `test_llm_port.py` because the E2 file is the E2's, and
what is proven here is a different claim: that the loop's vocabulary survives
the round trip. Three things must hold or the loop silently degrades into the
E2 (a model that is never offered a tool never asks for one):

  * a turn with no tools sends no `tools` key at all — the ceiling's last call
    depends on it, and "an empty list" is not the same request as "no list";
  * an assistant turn that asked for tools is echoed back WITH its request, and
    each answer carries the id it answers — providers reject the alternative,
    and mixing the ids up is a bug that only ever looks like a confused agent;
  * a tool-call turn has `content: null`, and null is not text.
"""

import json

import httpx

from agents_runtime.agent_core.llm import ChatRequest, Message, ToolCall, ToolSpec
from agents_runtime.agent_core.openrouter import OpenRouterLlm

GET_ORDER = ToolSpec(
    name="get_order",
    description="Consulta um pedido deste cliente.",
    parameters={
        "type": "object",
        "properties": {"order_id": {"type": "string"}},
        "required": [],
        "additionalProperties": False,
    },
)

A_TOOL_CALL = {
    "model": "anthropic/claude-sonnet-5",
    "choices": [
        {
            "message": {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {
                            "name": "get_order",
                            "arguments": '{"order_id": "1001"}',
                        },
                    }
                ],
            }
        }
    ],
    "usage": {"prompt_tokens": 120, "completion_tokens": 40, "cost": 0.00018},
}

A_PLAIN_REPLY = {
    "model": "anthropic/claude-sonnet-5",
    "choices": [{"message": {"content": "Seu pedido 1001 está pago 🧡"}}],
    "usage": {"prompt_tokens": 120, "completion_tokens": 40, "cost": 0.00018},
}


def capturing(payload: dict, seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=payload)

    return handler


def llm(handler) -> OpenRouterLlm:
    return OpenRouterLlm("sk-de-teste", transport=httpx.MockTransport(handler))


class TestWhatIsOffered:
    async def test_the_tools_travel_in_the_openai_shape(self) -> None:
        seen: dict = {}

        await llm(capturing(A_PLAIN_REPLY, seen)).chat(
            ChatRequest(
                model="claude-sonnet-5",
                messages=(Message(role="user", content="cadê meu pedido?"),),
                tools=(GET_ORDER,),
            )
        )

        assert seen["body"]["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "get_order",
                    "description": "Consulta um pedido deste cliente.",
                    "parameters": GET_ORDER.parameters,
                },
            }
        ]

    async def test_a_turn_with_no_tools_sends_no_tools_key(self) -> None:
        """The ceiling's last call is exactly this request, and it is what makes
        the loop terminate: a model with nothing to call has nothing to do but
        answer. An empty list is not the same request as no list."""
        seen: dict = {}

        await llm(capturing(A_PLAIN_REPLY, seen)).chat(
            ChatRequest(
                model="claude-sonnet-5",
                messages=(Message(role="user", content="oi"),),
            )
        )

        assert "tools" not in seen["body"]


class TestTheConversationGoingBack:
    async def test_an_assistant_turn_carries_the_request_it_made(self) -> None:
        seen: dict = {}
        call = ToolCall(id="call_abc", name="get_order", arguments='{"order_id": "1001"}')

        await llm(capturing(A_PLAIN_REPLY, seen)).chat(
            ChatRequest(
                model="claude-sonnet-5",
                messages=(
                    Message(role="user", content="cadê meu pedido?"),
                    Message(role="assistant", content="", tool_calls=(call,)),
                    Message(
                        role="tool",
                        content='{"success": true}',
                        tool_call_id="call_abc",
                    ),
                ),
            )
        )

        assert seen["body"]["messages"][1] == {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "get_order", "arguments": '{"order_id": "1001"}'},
                }
            ],
        }
        assert seen["body"]["messages"][2] == {
            "role": "tool",
            "content": '{"success": true}',
            "tool_call_id": "call_abc",
        }

    async def test_an_ordinary_turn_gains_no_extra_keys(self) -> None:
        """The E2 request shape is unchanged. A `tool_calls: []` on every user
        message would be a different request to every provider that reads it."""
        seen: dict = {}

        await llm(capturing(A_PLAIN_REPLY, seen)).chat(
            ChatRequest(
                model="claude-sonnet-5",
                messages=(Message(role="user", content="oi"),),
            )
        )

        assert seen["body"]["messages"] == [{"role": "user", "content": "oi"}]


class TestWhatComesBack:
    async def test_a_requested_tool_arrives_whole(self) -> None:
        result = await llm(capturing(A_TOOL_CALL, {})).chat(
            ChatRequest(
                model="claude-sonnet-5",
                messages=(Message(role="user", content="cadê meu pedido?"),),
                tools=(GET_ORDER,),
            )
        )

        assert result.tool_calls == (
            ToolCall(id="call_abc", name="get_order", arguments='{"order_id": "1001"}'),
        )

    async def test_the_arguments_stay_raw_text(self) -> None:
        """Parsed here, a malformed call would become either an exception that
        costs the customer their reply or an empty mapping that runs the tool
        with its defaults. The parse belongs where the failure can be handed
        back to the model."""
        broken = {
            **A_TOOL_CALL,
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_x",
                                "type": "function",
                                "function": {"name": "get_order", "arguments": "{pedido"},
                            }
                        ],
                    }
                }
            ],
        }

        result = await llm(capturing(broken, {})).chat(
            ChatRequest(
                model="claude-sonnet-5",
                messages=(Message(role="user", content="oi"),),
                tools=(GET_ORDER,),
            )
        )

        assert result.tool_calls[0].arguments == "{pedido"

    async def test_a_null_content_is_empty_text_not_none(self) -> None:
        """`ChatResult.text` is a string. A None reaching a prompt or a draft is
        the word "None" arriving at a customer."""
        result = await llm(capturing(A_TOOL_CALL, {})).chat(
            ChatRequest(
                model="claude-sonnet-5",
                messages=(Message(role="user", content="oi"),),
                tools=(GET_ORDER,),
            )
        )

        assert result.text == ""

    async def test_a_reply_without_tool_calls_has_none(self) -> None:
        result = await llm(capturing(A_PLAIN_REPLY, {})).chat(
            ChatRequest(
                model="claude-sonnet-5",
                messages=(Message(role="user", content="oi"),),
                tools=(GET_ORDER,),
            )
        )

        assert result.tool_calls == ()
        assert result.text == "Seu pedido 1001 está pago 🧡"

"""The LLM port, proven against a fake transport.

`httpx.MockTransport` keeps everything in-process — no socket is opened, so
this is honestly `unit`. What CANNOT be proven here is OpenRouter's side of the
contract (does it really return `usage.cost`? does it accept `reasoning` for
every model?) — that is the `contract` suite, run on demand with a real key.

D1 (decisão 79) is what this file guards: every LLM call — chat and embedding —
leaves through OpenRouter, and the model is a STRING that comes from the
tenant's `agent_versions` row. An adapter that knew the model name would make
`model` in the schema decoration.

The base URL is imported, never typed: `tests/unit/test_no_provider_network.py`
fails the build if a provider host appears in a test outside `tests/contract/`.
"""

import json

import httpx
import pytest

from agents_runtime.agent_core.llm import ChatRequest, Message
from agents_runtime.agent_core.openrouter import BASE_URL, OpenRouterLlm, from_env
from agents_runtime.queueing.failures import Failure, classify

A_REPLY = {
    "model": "anthropic/claude-sonnet-5",
    "choices": [{"message": {"content": "Oi! Já vou verificar seu pedido 🧡"}}],
    "usage": {"prompt_tokens": 120, "completion_tokens": 40, "cost": 0.00018},
}


def llm_answering(handler) -> OpenRouterLlm:
    return OpenRouterLlm("sk-de-teste", transport=httpx.MockTransport(handler))


def a_request(**overrides) -> ChatRequest:
    defaults = dict(
        model="claude-sonnet-5",
        messages=(
            Message(role="system", content="Você é o atendente da loja."),
            Message(role="user", content="cadê meu pedido?"),
        ),
    )
    return ChatRequest(**{**defaults, **overrides})


def replying(payload: dict, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


class TestTheRequest:
    async def test_the_request_is_the_openai_compatible_one(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=A_REPLY)

        result = await llm_answering(handler).chat(a_request())

        assert result.text == "Oi! Já vou verificar seu pedido 🧡"
        assert seen["url"] == f"{BASE_URL}/chat/completions"
        assert seen["auth"] == "Bearer sk-de-teste"
        assert seen["body"]["messages"] == [
            {"role": "system", "content": "Você é o atendente da loja."},
            {"role": "user", "content": "cadê meu pedido?"},
        ]
        # Cost only comes back when it is asked for. Without this the `usage`
        # block has tokens and no price, `llm_calls.cost_usd` is NULL forever,
        # and the only alternative left would be a price table in the code.
        assert seen["body"]["usage"] == {"include": True}

    async def test_the_model_comes_from_the_caller_never_from_the_adapter(self) -> None:
        """D1: the model is per-tenant config. Two tenants, two strings, one adapter."""
        asked: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            asked.append(json.loads(request.content)["model"])
            return httpx.Response(200, json=A_REPLY)

        llm = llm_answering(handler)
        await llm.chat(a_request(model="claude-sonnet-5"))
        await llm.chat(a_request(model="openai/gpt-5"))

        assert asked == ["claude-sonnet-5", "openai/gpt-5"]

    async def test_extended_reasoning_is_asked_for_only_when_the_gate_said_so(self) -> None:
        """The think-gate (S4) decides; the adapter obeys. Asking for reasoning
        on every call is exactly the cost the gate exists to avoid."""
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json=A_REPLY)

        llm = llm_answering(handler)
        await llm.chat(a_request(think=True))
        await llm.chat(a_request(think=False))

        assert bodies[0]["reasoning"] == {"enabled": True}
        assert "reasoning" not in bodies[1]


class TestWhatComesBack:
    async def test_the_usage_is_the_providers_own_numbers(self) -> None:
        """Cost comes from the invoice, never from a price table in the code:
        a table of prices rots silently and the trail starts lying."""
        result = await llm_answering(replying(A_REPLY)).chat(a_request())

        assert result.usage.input_tokens == 120
        assert result.usage.output_tokens == 40
        assert result.usage.cost_usd == pytest.approx(0.00018)
        # What was BILLED, not what was asked for: the caller says
        # `claude-sonnet-5` and OpenRouter routes to a fully qualified name.
        assert result.model == "anthropic/claude-sonnet-5"
        assert result.provider == "openrouter"

    async def test_a_missing_cost_is_absent_not_zero(self) -> None:
        """Zero would read as "this call was free" on the cost screen."""
        without_cost = {**A_REPLY, "usage": {"prompt_tokens": 5, "completion_tokens": 1}}

        result = await llm_answering(replying(without_cost)).chat(a_request())

        assert result.usage.cost_usd is None
        assert result.usage.input_tokens == 5

    async def test_a_2xx_without_a_choice_is_permanent(self) -> None:
        """A success the provider cannot fill is a contract violation. Retrying
        it burns the ladder on a call that will never produce text."""
        with pytest.raises(ValueError) as failure:
            await llm_answering(replying({"choices": []})).chat(a_request())

        assert classify(failure.value) is Failure.PERMANENT


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "expected_class"),
        [
            (429, Failure.TRANSIENT),
            (500, Failure.TRANSIENT),
            (503, Failure.TRANSIENT),
            (400, Failure.PERMANENT),
            (401, Failure.PERMANENT),
        ],
    )
    async def test_provider_errors_speak_the_classifier_language(
        self, status: int, expected_class: Failure
    ) -> None:
        # Same contract the Cloud API adapter signs (unidade 4): the classifier
        # reads the HTTP status out of the message. One vocabulary, one judge.
        handler = replying({"error": {"message": "nope"}}, status=status)

        with pytest.raises(RuntimeError) as failure:
            await llm_answering(handler).chat(a_request())

        assert classify(failure.value) is expected_class

    def test_missing_credentials_die_at_startup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Doctrine of decisão 67: absent and broken are different states, and
        # neither may quietly become "the agent answers without a model".
        monkeypatch.delenv("AGENTS_OPENROUTER_API_KEY", raising=False)

        with pytest.raises(RuntimeError, match="AGENTS_OPENROUTER_API_KEY"):
            from_env()


class TestEmbeddings:
    """D1 again: chat and embedding leave through the SAME door. A second
    provider for embeddings would be a second key, a second failure mode and a
    second host in the network lock."""

    async def test_one_vector_per_text_in_the_order_asked(self) -> None:
        # The API is free to answer out of order — `index` is the authority.
        # Trusting arrival order would silently pair chunk A with vector B.
        payload = {
            "model": "openai/text-embedding-3-small",
            "data": [
                {"index": 1, "embedding": [0.4, 0.5, 0.6]},
                {"index": 0, "embedding": [0.1, 0.2, 0.3]},
            ],
            "usage": {"prompt_tokens": 8},
        }

        result = await llm_answering(replying(payload)).embed(
            ["entrega em todo o Brasil", "trocas em 7 dias"],
            model="openai/text-embedding-3-small",
        )

        assert result.vectors == ((0.1, 0.2, 0.3), (0.4, 0.5, 0.6))
        assert result.usage.input_tokens == 8

    async def test_the_embedding_request_carries_the_texts_and_the_model(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}]})

        await llm_answering(handler).embed(["oi"], model="openai/text-embedding-3-small")

        assert seen["url"] == f"{BASE_URL}/embeddings"
        assert seen["body"] == {"model": "openai/text-embedding-3-small", "input": ["oi"]}

    async def test_a_missing_vector_is_permanent(self) -> None:
        """Two texts in, one vector out: pairing them would attach a chunk to
        the wrong embedding, and the mistake only shows up as bad retrieval."""
        payload = {"data": [{"index": 0, "embedding": [0.1]}]}

        with pytest.raises(ValueError) as failure:
            await llm_answering(replying(payload)).embed(["a", "b"], model="e5")

        assert classify(failure.value) is Failure.PERMANENT

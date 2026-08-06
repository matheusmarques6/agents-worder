"""The LLM port — the shape everything that talks to a model speaks.

`arquitetura §3` gives `agent_core` the LLM, so the port lives here and not in a
module of its own. What arrives at a call site is always this Protocol, never an
adapter: the eval harness (S3) drives a scripted stand-in through it, the
responder (S9) drives the real one, and neither knows the difference.

D1 (decisão 79) is written into the types:

  * `model` is a STRING carried by the request, because it is per-tenant
    configuration (`agent_versions.model`). An adapter that chose the model
    would make that column decoration;
  * chat and embedding leave through the SAME door. A second provider for
    embeddings would mean a second key, a second failure mode and a second host
    in the network lock;
  * `Usage` holds what the provider says it billed. Cost is never computed here
    — a price table in the code rots silently and the trail starts lying.

`think` comes from the think-gate (S4), already decided before this point: the
port asks for extended reasoning, it never decides that it is warranted.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

#: Everything routes through OpenRouter (D1). Recorded on every row of
#: `internal.llm_calls` next to the model, because the model varies per tenant
#: and the route does not.
PROVIDER = "openrouter"

#: D2, and platform-fixed — unlike the chat model, this one is NOT per tenant:
#: the dimension is frozen in the schema as `vector(1536)`, so a tenant able to
#: choose another embedding model would be a tenant able to break its own
#: ingestion. Confirmed against the provider by the `contract` suite.
EMBEDDING_MODEL = "openai/text-embedding-3-small"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """What the model is told a tool is, and nothing more.

    The description is PT-BR because the model reads it alongside a Brazilian
    conversation — it is content, like the prompt, not code. `parameters` is a
    JSON Schema for the ARGUMENTS OBJECT only: what the model may name. It is
    never a statement about what the tool may reach, because the model's
    arguments are hostile input and scope comes from the job.
    """

    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool the model asked for.

    `arguments` is the RAW text the provider sent, not a parsed mapping, and
    that is deliberate. A model is perfectly capable of emitting text that is
    not JSON, and parsing it here would make a malformed call either an
    exception that costs the customer their reply or — worse — an empty mapping
    that reads as "no arguments" and runs the tool anyway. The parse belongs
    where the failure can be handed back to the model to fix.
    """

    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class Message:
    role: str
    content: str
    #: Only on an `assistant` turn that asked for tools. The provider requires
    #: its own request echoed back before it will read the answers.
    tool_calls: tuple[ToolCall, ...] = ()
    #: Only on a `tool` turn: which request this is the answer to.
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChatRequest:
    model: str
    messages: tuple[Message, ...]
    think: bool = False
    #: What the model may choose from on THIS call. Empty means "answer with
    #: what you have" — which is how the loop's ceiling terminates.
    tools: tuple[ToolSpec, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Usage:
    """What the call cost, as the provider reported it. Absent stays absent —
    a zero would read as "this call was free" on the cost screen."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class ChatResult:
    text: str
    usage: Usage
    #: What was BILLED — the provider's fully qualified name, which is not
    #: always the string the caller asked for.
    model: str
    provider: str = PROVIDER
    #: Empty on every turn where the model answered instead of asking. A caller
    #: that ignores this field gets exactly the E2 behaviour.
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...]
    usage: Usage
    model: str
    provider: str = PROVIDER


class EmbedderPort(Protocol):
    """Narrower on purpose: a tool that retrieves knowledge has no business
    holding something that can also generate text."""

    async def embed(self, texts: Sequence[str], *, model: str) -> EmbeddingResult: ...


class LlmPort(EmbedderPort, Protocol):
    async def chat(self, request: ChatRequest) -> ChatResult: ...

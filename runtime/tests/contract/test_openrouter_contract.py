"""The one suite that talks to OpenRouter for real — never on a gate.

`core/testes-e-cicd.md` §2: blocking suites (PR, merge) never touch the
external network. This level exists for the questions a fake transport cannot
answer, and it answers them on demand, with a real key, costing real cents.

Two questions are asked here because the rest of the system assumes their
answers:

  1. **Does OpenRouter report `usage.cost`?** Our cost trail records what the
     provider says it billed instead of a price table in the code (decisão do
     S5). If cost stops coming back, `llm_calls.cost_usd` silently becomes a
     column full of NULLs and the cost screen quietly stops working.

  2. **Is `text-embedding-3-small` still 1536 dimensions?** D2 froze that
     number into the schema as `vector(1536)`. A provider that changes it turns
     every knowledge ingestion into a failed INSERT — and this is the only
     place that can notice before that happens in production.

Skipped without a key, so a developer without one is never blocked. It is not
in `pr.yml` and must never be.
"""

import os

import pytest

from agents_runtime.agent_core.llm import ChatRequest, Message
from agents_runtime.agent_core.openrouter import from_env

EMBEDDING_MODEL = "openai/text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536  # D2, frozen in the schema as vector(1536)

CHAT_MODEL = "anthropic/claude-haiku-4.5"  # the cheapest honest round trip

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENTS_OPENROUTER_API_KEY"),
    reason="AGENTS_OPENROUTER_API_KEY not set — the contract suite needs a real key",
)


async def test_the_provider_reports_what_it_billed() -> None:
    result = await from_env().chat(
        ChatRequest(
            model=CHAT_MODEL,
            messages=(Message(role="user", content="Responda apenas: ok"),),
        )
    )

    assert result.text
    assert result.usage.input_tokens and result.usage.output_tokens
    assert result.usage.cost_usd is not None, (
        "OpenRouter stopped reporting usage.cost — llm_calls.cost_usd would go all NULL"
    )


async def test_the_embedding_still_has_the_dimension_the_schema_froze() -> None:
    result = await from_env().embed(["entrega em todo o Brasil"], model=EMBEDDING_MODEL)

    (vector,) = result.vectors
    assert len(vector) == EMBEDDING_DIMENSIONS, (
        f"{EMBEDDING_MODEL} now returns {len(vector)} dimensions; the schema is "
        f"vector({EMBEDDING_DIMENSIONS}) and every ingestion would fail at INSERT"
    )

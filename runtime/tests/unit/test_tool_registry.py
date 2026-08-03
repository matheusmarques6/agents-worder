"""The per-tenant toolset — what `agent_versions.enabled_tools` actually buys.

`arquitetura §3` calls `tools` a registry from which each tenant picks a subset,
and the E2 subset is two (§4 of the plano E2). The registry exists so that
"this tenant may not look up customers" is enforced where the tools are built,
not by hoping the prompt discouraged it — a tool absent from the registry
cannot be called however convincing the contact's message was.
"""

import pytest

from agents_runtime.tools.registry import AVAILABLE, build_registry
from tests.support.llm import EmbeddingStandIn


def _registry(*enabled: str):
    return build_registry(enabled, embedder=EmbeddingStandIn())


def test_a_tenant_gets_exactly_the_tools_it_enabled() -> None:
    registry = _registry("search_knowledge")

    assert set(registry) == {"search_knowledge"}


def test_a_tenant_with_no_tools_gets_none() -> None:
    """Legitimate: an agent that only talks. It must not silently inherit the
    catalogue because somebody thought an empty list meant "not configured"."""
    assert _registry() == {}


def test_an_unknown_tool_dies_at_composition() -> None:
    """A typo in a config row should fail when the agent is built, not when a
    customer is waiting for an answer."""
    with pytest.raises(ValueError, match="get_orders"):
        _registry("search_knowledge", "get_orders")


def test_the_catalogue_is_the_two_tools_of_the_milestone() -> None:
    """Tools that reach orders arrive in E3 with the order tables. A third name
    appearing here without its tables is the mistake this test catches."""
    assert set(AVAILABLE) == {"search_knowledge", "get_customer_context"}


def test_every_tool_answers_by_its_own_name() -> None:
    """`tool_calls.tool_name` is written from the tool, not from the key the
    registry was looked up by — the two drifting apart would make the trail
    name the wrong tool."""
    registry = _registry("search_knowledge", "get_customer_context")

    assert {name: tool.name for name, tool in registry.items()} == {
        "search_knowledge": "search_knowledge",
        "get_customer_context": "get_customer_context",
    }

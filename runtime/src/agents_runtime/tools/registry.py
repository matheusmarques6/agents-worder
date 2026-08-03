"""The per-tenant toolset — `arquitetura §3`: a registry, a subset per tenant.

The subset is data (`agent_versions.enabled_tools`), and it is enforced HERE
rather than in the prompt. A tool the tenant did not enable is not in the
registry, so no message — however convincing — can reach it. A prompt that
merely asks the model not to use something is a request; an absent tool is a
fact.

An unknown name is an error at build time, not at call time: a typo in a config
row has to fail when the agent is assembled, never while a customer waits.
"""

from collections.abc import Iterable

from agents_runtime.agent_core.llm import EmbedderPort
from agents_runtime.tools.base import Tool
from agents_runtime.tools.customer import GetCustomerContext
from agents_runtime.tools.knowledge import SearchKnowledge

#: The catalogue of the milestone (plano E2 §4). Tools that reach orders arrive
#: in E3, with the tables that make them possible.
AVAILABLE = ("search_knowledge", "get_customer_context")


def build_registry(enabled: Iterable[str], *, embedder: EmbedderPort) -> dict[str, Tool]:
    names = tuple(enabled)

    unknown = sorted(set(names) - set(AVAILABLE))
    if unknown:
        raise ValueError(
            f"unknown tools in the tenant's configuration: {', '.join(unknown)} "
            f"(available: {', '.join(AVAILABLE)})"
        )

    builders = {
        "search_knowledge": lambda: SearchKnowledge(embedder),
        "get_customer_context": GetCustomerContext,
    }
    return {name: builders[name]() for name in names}

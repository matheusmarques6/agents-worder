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
from agents_runtime.tools.consent import RecordOptout
from agents_runtime.tools.customer import GetCustomerContext
from agents_runtime.tools.knowledge import SearchKnowledge

#: The catalogue of the milestone (plano E2 §4). Tools that reach orders arrive
#: in E3, with the tables that make them possible.
AVAILABLE = ("search_knowledge", "get_customer_context")

#: Tools that are NOT a choice — the same principle that makes the Judge 1 model
#: platform-fixed: a safety gate does not weaken by customer configuration
#: (`CLAUDE.md`, canonical defaults).
#:
#: `record_optout` is the contact's right to refuse (RF-033c, RNF-044). A
#: merchant who could switch it off in `agent_versions.enabled_tools` would be
#: switching off somebody else's ability to say no, and the merchant is not the
#: party that right belongs to. So it is not in `AVAILABLE`: it is not something
#: a tenant picks, and naming it in a config row is a mistake worth failing on
#: rather than a preference worth honouring.
MANDATORY = ("record_optout",)


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


def build_toolset(enabled: Iterable[str], *, embedder: EmbedderPort) -> dict[str, Tool]:
    """What the agent actually holds: the mandatory tools, plus the chosen ones.

    Two functions rather than one because they answer different questions.
    `build_registry` answers "what did this tenant pick?", and its answer is
    allowed to be empty — an agent that only talks is a legitimate
    configuration. This one answers "what may this agent do?", and its answer is
    never empty, because honouring an opt-out is not a feature of some plans.

    The mandatory half is written FIRST so a tenant's configuration cannot shadow
    it even if `AVAILABLE` and `MANDATORY` ever overlap by accident.
    """
    return {"record_optout": RecordOptout(), **build_registry(enabled, embedder=embedder)}

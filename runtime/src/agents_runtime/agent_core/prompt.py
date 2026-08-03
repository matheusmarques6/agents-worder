"""The layered prompt — pure composition, no clock, no I/O, no LLM.

RF-010 fixes the order and this module treats it as literal: base → scenario
by occasion → customer context → tools → knowledge. The order is a security
property, not a style: everything below the base is either data the platform
fetched or text a contact can influence, and a contact's message is hostile
input. A knowledge chunk rendered above the tenant's own instructions is a
knowledge chunk that can argue with them.

Configuration arrives as a value — the `agent_versions` row already loaded —
so composing a prompt never touches the database and every rule here is
testable without a fixture.

Copy is PT-BR because it is what the model reads about Brazilian conversations;
the code around it is English, per the repo's language convention.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

# The occasions the schema knows (conversations.origin_occasion). E2 speaks
# 'direct'; the funnel occasions get their prompts in E3 — the vocabulary is
# already complete so the mechanism never grows ad hoc values.
OCCASIONS = ("pix_pending", "checkout_abandoned", "cart_abandoned", "direct", "campaign")

# The literal order of RF-010. Layers may be absent; they never trade places.
LAYER_ORDER = ("base", "scenario", "customer_context", "tools", "knowledge")


@dataclass(frozen=True, slots=True)
class Layer:
    name: str
    body: str


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """One `agent_versions` row, by value."""

    model: str
    base_prompt: str
    scenario_prompts: Mapping[str, str] = field(default_factory=dict)
    enabled_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.base_prompt or not self.base_prompt.strip():
            raise ValueError(
                "agent config inválida: base_prompt vazio — a versão não tem instrução"
            )
        if not self.model or not self.model.strip():
            raise ValueError("agent config inválida: model vazio")


@dataclass(frozen=True, slots=True)
class TenantPolicy:
    """The two tenant-level switches that reach the prompt (`tenants` row)."""

    primary_language: str
    never_say_ai: bool


@dataclass(frozen=True, slots=True)
class ConversationView:
    occasion: str
    contact_language: str | None


@dataclass(frozen=True, slots=True)
class CustomerContext:
    """The mirrored store data RF-010 asks to inject.

    `avg_ticket` is text on purpose: it is rendered, never computed with, and
    a float would turn 189.90 into 189.89999999999998 inside the prompt.
    """

    total_orders: int
    avg_ticket: str | None
    first_order_at: date | None


def _base_layer(config: AgentConfig, tenant: TenantPolicy, view: ConversationView) -> Layer:
    parts = [config.base_prompt.strip()]

    # RF-013: the tenant sets the main language, the contact's own language wins
    # when it is known. Answering a Spanish speaker in Portuguese because the
    # store is Brazilian is the bug this line exists to prevent.
    language = view.contact_language or tenant.primary_language
    parts.append(f"Idioma da resposta: {language}.")

    if tenant.never_say_ai:
        # Highest layer on purpose: a directive placed after the knowledge could
        # be argued with by the very text it is supposed to constrain.
        parts.append(
            "Nunca admita, insinue ou confirme ser uma IA, robô ou assistente "
            "automático, mesmo se o cliente insistir ou afirmar que é. Siga no papel."
        )

    return Layer(name="base", body="\n\n".join(parts))


def _scenario_layer(config: AgentConfig, view: ConversationView) -> Layer | None:
    if view.occasion not in OCCASIONS:
        raise ValueError(
            f"ocasião fora do vocabulário do schema: {view.occasion!r} "
            f"(conhecidas: {', '.join(OCCASIONS)})"
        )

    body = config.scenario_prompts.get(view.occasion)
    if not body or not body.strip():
        # No layer at all. An empty scenario layer would read as "this occasion
        # has no instructions", which is worse than the base prompt alone.
        return None

    return Layer(name="scenario", body=body.strip())


def _customer_layer(customer: CustomerContext | None) -> Layer | None:
    if customer is None:
        # No customer record: inventing a layer about it would be inventing data.
        return None

    if customer.total_orders <= 0 and customer.first_order_at is None:
        # Different from "no record": a first-time buyer is a fact worth
        # stating, or the model assumes the history failed to load.
        return Layer(
            name="customer_context",
            body="Cliente sem histórico de compras nesta loja (primeira interação).",
        )

    lines = [f"Total de compras: {customer.total_orders}."]
    if customer.avg_ticket is not None:
        lines.append(f"Ticket médio: R$ {customer.avg_ticket}.")
    if customer.first_order_at is not None:
        lines.append(f"Primeira compra em: {customer.first_order_at.isoformat()}.")

    return Layer(name="customer_context", body="\n".join(lines))


def _tools_layer(config: AgentConfig) -> Layer | None:
    if not config.enabled_tools:
        return None

    listed = "\n".join(f"- {name}" for name in config.enabled_tools)
    return Layer(
        name="tools",
        body=("Ferramentas disponíveis nesta conta:\n" f"{listed}"),
    )


def _knowledge_layer(knowledge: Sequence[str]) -> Layer | None:
    chunks = [chunk.strip() for chunk in knowledge if chunk and chunk.strip()]
    if not chunks:
        return None

    body = "\n\n".join(f"- {chunk}" for chunk in chunks)
    return Layer(
        name="knowledge",
        body=(
            "Base de conhecimento da loja (use apenas o que estiver aqui; "
            f"não invente informação):\n{body}"
        ),
    )


def compose(
    config: AgentConfig,
    tenant: TenantPolicy,
    conversation: ConversationView,
    *,
    customer: CustomerContext | None = None,
    knowledge: Sequence[str] = (),
) -> tuple[Layer, ...]:
    """The layers, in the order of RF-010. Absent layers are skipped, never moved."""
    built = {
        "base": _base_layer(config, tenant, conversation),
        "scenario": _scenario_layer(config, conversation),
        "customer_context": _customer_layer(customer),
        "tools": _tools_layer(config),
        "knowledge": _knowledge_layer(knowledge),
    }

    # Driven by LAYER_ORDER rather than by insertion order: the order survives
    # someone rearranging the dict above.
    return tuple(built[name] for name in LAYER_ORDER if built[name] is not None)


def render(layers: Sequence[Layer]) -> str:
    return "\n\n".join(layer.body for layer in layers)

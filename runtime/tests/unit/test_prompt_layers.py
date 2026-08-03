"""E2 · S4 — the layered prompt, pure.

One test per rule of the `agent_core` row in core/testes-e-cicd.md §2:
layer order, scenario selection by `origin_occasion`, customer context
injection, language adaptation and `never_say_ai`. The think-gate is the sixth
rule and lives in its own file.

The order is asserted **literally** (RF-010): base → scenario → customer
context → tools → knowledge. It is not a stylistic preference — a knowledge
chunk that lands above the tenant's own instructions is a knowledge chunk that
can argue with them, and a contact's message is hostile input to the model.

Everything here is pure: config is the `agent_versions` row passed by value,
there is no clock, no database and no LLM.
"""

from datetime import date

import pytest

from agents_runtime.agent_core.prompt import (
    LAYER_ORDER,
    AgentConfig,
    ConversationView,
    CustomerContext,
    TenantPolicy,
    compose,
    render,
)

A_CONFIG = AgentConfig(
    model="claude-sonnet-5",
    base_prompt="Você é a Ana, atendente da Loja do Bruno.",
    scenario_prompts={
        "pix_pending": "O cliente tem um PIX gerado e não pago.",
        "direct": "O cliente chamou por conta própria.",
    },
    enabled_tools=("search_knowledge", "get_customer_context"),
)

A_TENANT = TenantPolicy(primary_language="pt-BR", never_say_ai=True)

A_CONVERSATION = ConversationView(occasion="direct", contact_language=None)

A_CUSTOMER = CustomerContext(
    total_orders=4,
    avg_ticket="189.90",
    first_order_at=date(2025, 3, 14),
)


def layers_by_name(layers) -> dict:
    return {layer.name: layer.body for layer in layers}


# --- the order ----------------------------------------------------------------


def test_the_canonical_order_is_the_one_rf_010_wrote() -> None:
    # Spelled out, NOT compared against LAYER_ORDER: a test that asserts the
    # constant equals itself passes happily while the order is reversed. This
    # line is the one a sabotage has to break.
    assert LAYER_ORDER == ("base", "scenario", "customer_context", "tools", "knowledge")


def test_the_layers_come_in_the_canonical_order() -> None:
    layers = compose(
        A_CONFIG,
        A_TENANT,
        A_CONVERSATION,
        customer=A_CUSTOMER,
        knowledge=("Entregamos em todo o Brasil.",),
    )

    assert [layer.name for layer in layers] == [
        "base",
        "scenario",
        "customer_context",
        "tools",
        "knowledge",
    ]


def test_the_base_layer_is_always_first() -> None:
    # The security half of the order: everything below the base is either data
    # the platform fetched or text a contact can influence. The tenant's own
    # instructions, and the never_say_ai directive with them, come first.
    layers = compose(
        A_CONFIG,
        A_TENANT,
        A_CONVERSATION,
        customer=A_CUSTOMER,
        knowledge=("Entregamos em todo o Brasil.",),
    )

    assert layers[0].name == "base"
    assert layers[-1].name == "knowledge"


def test_render_keeps_the_order_in_the_text() -> None:
    # The tuple could be ordered and the rendering still shuffle it. This is the
    # assertion that survives a refactor of `render`.
    layers = compose(
        A_CONFIG,
        A_TENANT,
        A_CONVERSATION,
        customer=A_CUSTOMER,
        knowledge=("Entregamos em todo o Brasil.",),
    )

    text = render(layers)
    positions = [text.index(layer.body) for layer in layers]

    assert positions == sorted(positions)


def test_a_missing_layer_never_reorders_the_others() -> None:
    # No customer, no knowledge: what is left keeps its relative order instead
    # of collapsing into whatever order the code happened to build.
    layers = compose(A_CONFIG, A_TENANT, A_CONVERSATION, customer=None, knowledge=())

    names = [layer.name for layer in layers]

    assert names == [name for name in LAYER_ORDER if name in names]


# --- the scenario layer, selected by the occasion -----------------------------


def test_the_scenario_layer_is_the_one_of_the_conversations_occasion() -> None:
    conversation = ConversationView(occasion="pix_pending", contact_language=None)

    body = layers_by_name(compose(A_CONFIG, A_TENANT, conversation))["scenario"]

    assert body == A_CONFIG.scenario_prompts["pix_pending"]


def test_an_occasion_with_no_configured_prompt_drops_the_layer() -> None:
    # The base prompt already covers it. Shipping an empty scenario layer would
    # read to the model as "this occasion has no instructions" — worse than none.
    conversation = ConversationView(occasion="cart_abandoned", contact_language=None)

    assert "scenario" not in layers_by_name(compose(A_CONFIG, A_TENANT, conversation))


def test_an_occasion_outside_the_vocabulary_is_rejected() -> None:
    # Same doctrine as the webhook schemas: never "use what parsed". An unknown
    # occasion means the caller and the schema disagree.
    conversation = ConversationView(occasion="black-friday", contact_language=None)

    with pytest.raises(ValueError, match="black-friday"):
        compose(A_CONFIG, A_TENANT, conversation)


# --- the customer context -----------------------------------------------------


def test_the_context_layer_carries_orders_ticket_and_first_purchase() -> None:
    body = layers_by_name(
        compose(A_CONFIG, A_TENANT, A_CONVERSATION, customer=A_CUSTOMER)
    )["customer_context"]

    assert "4" in body
    assert "189.90" in body
    assert "2025-03-14" in body


def test_a_contact_with_no_history_is_declared_not_omitted() -> None:
    # A first-time buyer is a fact worth stating. Dropping the layer would let
    # the model assume the history simply failed to load.
    empty = CustomerContext(total_orders=0, avg_ticket=None, first_order_at=None)

    body = layers_by_name(compose(A_CONFIG, A_TENANT, A_CONVERSATION, customer=empty))[
        "customer_context"
    ]

    assert "sem histórico" in body


def test_without_a_customer_the_layer_does_not_exist() -> None:
    # Different from "no history": here the platform has no customer record at
    # all, and inventing a layer about it would be inventing data.
    layers = layers_by_name(compose(A_CONFIG, A_TENANT, A_CONVERSATION, customer=None))

    assert "customer_context" not in layers


# --- language adaptation (RF-013) ---------------------------------------------


def test_the_tenant_language_rules_when_the_contact_language_is_unknown() -> None:
    body = layers_by_name(compose(A_CONFIG, A_TENANT, A_CONVERSATION))["base"]

    assert "pt-BR" in body


def test_the_contact_language_wins_when_it_differs() -> None:
    conversation = ConversationView(occasion="direct", contact_language="es-AR")

    body = layers_by_name(compose(A_CONFIG, A_TENANT, conversation))["base"]

    assert "es-AR" in body


# --- never_say_ai -------------------------------------------------------------


def test_never_say_ai_puts_its_directive_in_the_base_layer() -> None:
    # The base layer, not a later one: a directive that arrives after the
    # knowledge could be argued with by what it was supposed to constrain.
    body = layers_by_name(compose(A_CONFIG, A_TENANT, A_CONVERSATION))["base"]

    assert "nunca" in body.lower()
    assert "ia" in body.lower()


def test_with_never_say_ai_off_no_directive_is_added() -> None:
    permissive = TenantPolicy(primary_language="pt-BR", never_say_ai=False)

    body = layers_by_name(compose(A_CONFIG, permissive, A_CONVERSATION))["base"]

    assert "nunca admita" not in body.lower()


# --- tools and knowledge ------------------------------------------------------


def test_the_tools_layer_lists_exactly_the_enabled_tools() -> None:
    body = layers_by_name(compose(A_CONFIG, A_TENANT, A_CONVERSATION))["tools"]

    assert "search_knowledge" in body
    assert "get_customer_context" in body
    assert "get_order" not in body


def test_a_tenant_with_no_tools_gets_no_tools_layer() -> None:
    toolless = AgentConfig(
        model=A_CONFIG.model,
        base_prompt=A_CONFIG.base_prompt,
        scenario_prompts=A_CONFIG.scenario_prompts,
        enabled_tools=(),
    )

    assert "tools" not in layers_by_name(compose(toolless, A_TENANT, A_CONVERSATION))


def test_the_knowledge_layer_carries_the_retrieved_chunks() -> None:
    body = layers_by_name(
        compose(
            A_CONFIG,
            A_TENANT,
            A_CONVERSATION,
            knowledge=("Prazo de entrega: 5 dias úteis.", "Trocas em até 7 dias."),
        )
    )["knowledge"]

    assert "5 dias úteis" in body
    assert "Trocas em até 7 dias" in body


def test_retrieval_that_found_nothing_adds_no_layer() -> None:
    assert "knowledge" not in layers_by_name(
        compose(A_CONFIG, A_TENANT, A_CONVERSATION, knowledge=())
    )


# --- the config is a value ----------------------------------------------------


def test_an_empty_base_prompt_is_rejected() -> None:
    # A version with no base prompt would compose a prompt made only of context
    # and knowledge — an agent with no instructions at all.
    with pytest.raises(ValueError):
        AgentConfig(
            model="claude-sonnet-5",
            base_prompt="   ",
            scenario_prompts={},
            enabled_tools=(),
        )

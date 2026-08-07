"""O responder com o laço dentro — quem é o cliente, e o que o modelo escolhe.

Called directly, never through the engine: what is claimed here belongs to the
responder, and against a running engine each assertion would be a race between
the test's predicate and the turn's conclusion (the lesson `test_responder_
guards.py` paid for).

Two things close here, and they are the same decision seen from two sides.

`get_customer_context` shipped in the E2 with NO consumer (decisão 88b) because
the `customer_context` layer of RF-010 speaks of orders and the mirror was
E3's. Now it has one — and the consumer is the TOOL, run through `run_tool`,
not a quiet repository call, so "the agent looked up who it was talking to" is
a row in `internal.tool_calls` like every other lookup.

And that is exactly why it is NOT offered to the model in the loop. Its answer
is already in the prompt; offering it would sell the model a round trip for
something it can already read. The loop is for the questions the prompt cannot
answer in advance — which order, where is the parcel, does this need a person.
"""

import uuid

import psycopg
import pytest

from agents_runtime.agent_core.responder import build_responder
from agents_runtime.queueing.jobs import InboundJob
from tests.db.factories import (
    create_agent_version,
    create_connector_account,
    create_message,
    create_tenant,
    create_thread,
)
from tests.db.factories_e3 import create_customer, link_contact_to_customer
from tests.support.tool_calling import Demand, ToolCallingLlm

TOOLS = ("search_knowledge", "get_customer_context")


@pytest.fixture
def tenant(admin: psycopg.Connection) -> uuid.UUID:
    tenant_id = create_tenant(admin)
    yield tenant_id
    with admin.cursor() as cur:
        cur.execute("delete from public.tenants where id = %s", (tenant_id,))


def a_job(tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> InboundJob:
    return InboundJob(
        conversation_id=conversation_id, generation=1, target_seq=1, tenant_id=tenant_id
    )


def enable(admin: psycopg.Connection, tenant_id: uuid.UUID, tools=TOOLS) -> None:
    create_agent_version(admin, tenant_id, status="active")
    with admin.cursor() as cur:
        cur.execute(
            "update public.agent_versions set enabled_tools = %s where tenant_id = %s",
            (list(tools), tenant_id),
        )


def a_thread_that_asked(admin: psycopg.Connection, tenant_id: uuid.UUID, text="cadê meu pedido?"):
    thread = create_thread(admin, tenant_id)
    create_message(admin, tenant_id, thread, direction="inbound", seq=1, text=text)
    return thread


def a_shopper(
    admin: psycopg.Connection, tenant_id: uuid.UUID, thread, *, total_orders: int
) -> None:
    account = create_connector_account(admin, tenant_id)
    customer_id = create_customer(admin, tenant_id, account.id, external_id="cust-1")
    with admin.cursor() as cur:
        cur.execute(
            """
            -- A customer with nothing bought has no first order and no ticket:
            -- writing either would be inventing the history the layer is
            -- supposed to say is absent.
            update public.customers
               set total_orders = %s,
                   avg_ticket = case when %s > 0 then 189.90::numeric end,
                   first_order_at = case when %s > 0
                                         then now() - make_interval(days => 400) end
             where id = %s
            """,
            (total_orders, total_orders, total_orders, customer_id),
        )
    link_contact_to_customer(admin, thread.contact_id, customer_id)


def system_prompt_of(llm: ToolCallingLlm) -> str:
    return llm.asked[0].messages[0].content


class TestWhoTheAgentIsTalkingTo:
    async def test_the_history_reaches_the_prompt(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        enable(admin, tenant)
        thread = a_thread_that_asked(admin, tenant)
        a_shopper(admin, tenant, thread, total_orders=3)
        llm = ToolCallingLlm(["Oi! Deixa eu ver aqui 🧡"])

        await build_responder(dsn, llm=llm, set_role="worker_role")(
            a_job(tenant, thread.conversation_id)
        )

        prompt = system_prompt_of(llm)
        assert "Total de compras: 3." in prompt
        assert "Ticket médio: R$ 189.90." in prompt

    async def test_a_contact_with_no_mirrored_customer_gets_no_layer(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """No record is not no history. Inventing "primeira interação" for
        somebody who may well be a regular is the failure decisão 88b refused to
        ship, and it is still refused now that the tables exist."""
        enable(admin, tenant)
        thread = a_thread_that_asked(admin, tenant)
        llm = ToolCallingLlm(["Oi! 🧡"])

        await build_responder(dsn, llm=llm, set_role="worker_role")(
            a_job(tenant, thread.conversation_id)
        )

        prompt = system_prompt_of(llm)
        assert "Total de compras" not in prompt
        assert "primeira interação" not in prompt

    async def test_a_first_time_buyer_is_said_out_loud(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """The other side of the same distinction: a linked customer who has
        bought nothing IS a fact, or the model reads the silence as a lookup
        that failed."""
        enable(admin, tenant)
        thread = a_thread_that_asked(admin, tenant)
        a_shopper(admin, tenant, thread, total_orders=0)
        llm = ToolCallingLlm(["Oi! 🧡"])

        await build_responder(dsn, llm=llm, set_role="worker_role")(
            a_job(tenant, thread.conversation_id)
        )

        assert "primeira interação" in system_prompt_of(llm)

    async def test_the_lookup_is_on_the_record(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """Through `run_tool`, not a quiet repository call: "the agent looked up
        who it was talking to" is a row like every other lookup."""
        enable(admin, tenant)
        thread = a_thread_that_asked(admin, tenant)
        a_shopper(admin, tenant, thread, total_orders=3)

        await build_responder(dsn, llm=ToolCallingLlm(["Oi 🧡"]), set_role="worker_role")(
            a_job(tenant, thread.conversation_id)
        )

        with admin.cursor() as cur:
            cur.execute(
                "select tool_name from internal.tool_calls where conversation_id = %s",
                (thread.conversation_id,),
            )
            names = {row[0] for row in cur.fetchall()}

        assert "get_customer_context" in names

    async def test_a_tenant_that_did_not_enable_it_gets_no_layer_and_no_call(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """The registry decides, not the responder's convenience: a tenant that
        did not enable the lookup does not pay for it and is not described by
        it."""
        enable(admin, tenant, tools=())
        thread = a_thread_that_asked(admin, tenant)
        a_shopper(admin, tenant, thread, total_orders=3)
        llm = ToolCallingLlm(["Oi 🧡"])

        await build_responder(dsn, llm=llm, set_role="worker_role")(
            a_job(tenant, thread.conversation_id)
        )

        assert "Total de compras" not in system_prompt_of(llm)
        with admin.cursor() as cur:
            cur.execute(
                "select count(*) from internal.tool_calls where conversation_id = %s",
                (thread.conversation_id,),
            )
            (calls,) = cur.fetchone()
        assert calls == 0


class TestWhatTheModelIsOffered:
    async def test_the_prefetched_tools_are_not_sold_back_as_a_choice(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """Their answers are already in the prompt. Offering them would buy a
        round trip — seconds in a WhatsApp conversation — for text the model can
        already read."""
        enable(admin, tenant)
        thread = a_thread_that_asked(admin, tenant)
        llm = ToolCallingLlm(["Oi 🧡"])

        await build_responder(dsn, llm=llm, set_role="worker_role")(
            a_job(tenant, thread.conversation_id)
        )

        offered = {spec.name for spec in llm.asked[0].tools}
        assert "search_knowledge" not in offered
        assert "get_customer_context" not in offered

    async def test_the_tool_nobody_may_switch_off_is_always_offered(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """`record_optout` is mandatory in the registry (S6) and it is a real
        choice for the first time here: the model detects the intention, the
        tool performs the act."""
        enable(admin, tenant, tools=())
        thread = a_thread_that_asked(admin, tenant)
        llm = ToolCallingLlm(["Ok, não te mando mais nada."])

        await build_responder(dsn, llm=llm, set_role="worker_role")(
            a_job(tenant, thread.conversation_id)
        )

        assert {spec.name for spec in llm.asked[0].tools} == {"record_optout"}

    async def test_the_order_tools_a_tenant_enabled_reach_the_model(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """The catalogue widened in this step, and this is what it buys: a
        merchant who enabled the order tools has an agent that can actually be
        asked about an order. Enabled, not compulsory — unlike `record_optout`,
        whose right belongs to the contact, looking up a pedido protects nobody
        from the merchant."""
        enable(admin, tenant, tools=("get_order", "get_tracking", "escalate_to_human"))
        thread = a_thread_that_asked(admin, tenant)
        llm = ToolCallingLlm(["Deixa eu olhar 🧡"])

        await build_responder(dsn, llm=llm, set_role="worker_role")(
            a_job(tenant, thread.conversation_id)
        )

        assert {spec.name for spec in llm.asked[0].tools} == {
            "get_order",
            "get_tracking",
            "escalate_to_human",
            "record_optout",
        }

    async def test_a_tenant_that_did_not_enable_the_order_tools_cannot_be_talked_into_them(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """The negative that keeps the widening honest. A tool absent from the
        registry is absent from the offer AND from the lookup — however
        convincing the contact's message was, and however confidently the model
        names it."""
        enable(admin, tenant, tools=())
        thread = a_thread_that_asked(admin, tenant, text="me vê o pedido 1001")
        llm = ToolCallingLlm(
            [Demand(("get_order", '{"order_id": "1001"}')), "Não consigo ver isso por aqui."]
        )

        await build_responder(dsn, llm=llm, set_role="worker_role")(
            a_job(tenant, thread.conversation_id)
        )

        assert "get_order" not in {spec.name for spec in llm.asked[0].tools}
        with admin.cursor() as cur:
            cur.execute(
                """
                select count(*) from internal.tool_calls
                 where conversation_id = %s and tool_name = 'get_order'
                """,
                (thread.conversation_id,),
            )
            (ran,) = cur.fetchone()
        assert ran == 0

    async def test_a_tool_the_model_chose_runs_and_leaves_a_row(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        enable(admin, tenant, tools=())
        thread = a_thread_that_asked(admin, tenant, text="para de me mandar mensagem")
        llm = ToolCallingLlm(
            [Demand(("record_optout", "{}")), "Pronto, não te mando mais mensagens. 🧡"]
        )

        draft = await build_responder(dsn, llm=llm, set_role="worker_role")(
            a_job(tenant, thread.conversation_id)
        )

        assert draft == {"text": "Pronto, não te mando mais mensagens. 🧡"}
        with admin.cursor() as cur:
            cur.execute(
                """
                select tool_name, success from internal.tool_calls
                 where conversation_id = %s and tool_name = 'record_optout'
                """,
                (thread.conversation_id,),
            )
            (row,) = cur.fetchall()
            cur.execute(
                "select count(*) from public.suppression_list where contact_id = %s",
                (thread.contact_id,),
            )
            (suppressed,) = cur.fetchone()

        assert row == ("record_optout", True)
        # The tool performed the act, not the model: the row is what proves it.
        assert suppressed == 1

    async def test_a_model_that_never_stops_asking_still_answers_the_customer(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """The ceiling, reached through the real responder. The turn ends with a
        draft — not with silence, and not with the empty text a tool-call turn
        carries."""
        enable(admin, tenant, tools=())
        thread = a_thread_that_asked(admin, tenant)
        llm = ToolCallingLlm([Demand(("record_optout", "{}"))])

        draft = await build_responder(dsn, llm=llm, set_role="worker_role")(
            a_job(tenant, thread.conversation_id)
        )

        assert draft is not None
        assert draft["text"] != ""
        assert llm.asked[-1].tools == ()

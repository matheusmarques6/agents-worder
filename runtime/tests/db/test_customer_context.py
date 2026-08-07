"""`get_customer_context` gets the half it was built without.

The E2 shipped this tool with no consumer and said why (decisão 88b): the
`customer_context` layer of RF-010 speaks of PEDIDOS, the order mirror was E3's,
and inventing a "customer with no history" for somebody who had already talked
to the store three times would have been worse than the layer's absence.

The mirror exists now, so the tool answers about orders — and the whole point is
the distinction the E2 refused to fake:

  * a contact never linked to a store customer has NO RECORD. The tool says
    `orders: null`, and the prompt gets no layer at all;
  * a contact linked to a customer who has bought nothing has NO HISTORY. That
    is a fact worth stating, or the model reads the silence as a lookup that
    failed.

Same connection shape as every other tool suite here: autocommit, role set, no
tenant scope, because a pre-scoped connection would hide a tool that forgot to
scope itself.
"""

import uuid

import psycopg
import pytest

from agents_runtime.tools import base as tools
from agents_runtime.tools.customer import GetCustomerContext
from tests.db.factories import create_connector_account, create_tenant, create_thread
from tests.db.factories_e3 import create_customer, link_contact_to_customer
from tests.support.clock import FrozenClock
from tests.support.database import as_runtime_worker
from tests.support.llm import START


@pytest.fixture
def tenant(admin: psycopg.Connection) -> uuid.UUID:
    tenant_id = create_tenant(admin)
    yield tenant_id
    with admin.cursor() as cur:
        cur.execute("delete from public.tenants where id = %s", (tenant_id,))


async def ask(dsn: str, tenant_id: uuid.UUID, conversation_id: uuid.UUID):
    async with as_runtime_worker(dsn) as conn:
        return await tools.run_tool(
            conn,
            GetCustomerContext(),
            tools.ToolContext(tenant_id=tenant_id, conversation_id=conversation_id),
            {},
            clock=FrozenClock(START),
        )


def a_mirrored_customer(
    admin: psycopg.Connection,
    tenant_id: uuid.UUID,
    conversation,
    *,
    total_orders: int,
    total_spent: str | None = None,
    avg_ticket: str | None = None,
    first_order_ago_days: int | None = None,
) -> None:
    account = create_connector_account(admin, tenant_id)
    customer_id = create_customer(admin, tenant_id, account.id, external_id="cust-1")
    with admin.cursor() as cur:
        cur.execute(
            """
            update public.customers
               set total_orders = %s,
                   total_spent = coalesce(%s::numeric, 0),
                   avg_ticket = %s::numeric,
                   first_order_at = case when %s::integer is null then null
                                         else now() - make_interval(days => %s::integer) end
             where id = %s
            """,
            (
                total_orders,
                total_spent,
                avg_ticket,
                first_order_ago_days,
                first_order_ago_days,
                customer_id,
            ),
        )
    link_contact_to_customer(admin, conversation.contact_id, customer_id)


async def test_a_contact_never_linked_to_a_customer_has_no_record(
    dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
) -> None:
    """`orders: null`, not `orders: {total: 0}`. The second would tell the model
    that somebody who may well be a regular has never bought anything."""
    thread = create_thread(admin, tenant)

    result = await ask(dsn, tenant, thread.conversation_id)

    assert result.success is True
    assert result.output["orders"] is None


async def test_a_linked_customer_who_has_bought_nothing_is_a_first_time_buyer(
    dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
) -> None:
    thread = create_thread(admin, tenant)
    a_mirrored_customer(admin, tenant, thread, total_orders=0)

    result = await ask(dsn, tenant, thread.conversation_id)

    assert result.output["orders"] == {
        "total": 0,
        "avg_ticket": None,
        "first_order_at": None,
    }


async def test_a_customer_with_history_brings_it(
    dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
) -> None:
    thread = create_thread(admin, tenant)
    a_mirrored_customer(
        admin,
        tenant,
        thread,
        total_orders=3,
        total_spent="569.70",
        avg_ticket="189.90",
        first_order_ago_days=400,
    )

    result = await ask(dsn, tenant, thread.conversation_id)

    assert result.output["orders"]["total"] == 3
    # Text, never a float: 189.90 through a float is 189.89999999999998 in the
    # prompt and in `internal.tool_calls` alike.
    assert result.output["orders"]["avg_ticket"] == "189.90"
    assert result.output["orders"]["first_order_at"] is not None


async def test_the_history_of_another_tenants_customer_is_never_reached(
    dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
) -> None:
    """The link is a column on OUR contact, so the leak this guards against is
    the join reaching a `customers` row RLS should have hidden."""
    stranger = create_tenant(admin)
    try:
        theirs = create_thread(admin, stranger)
        a_mirrored_customer(admin, stranger, theirs, total_orders=9)

        result = await ask(dsn, tenant, theirs.conversation_id)

        assert result.success is False
        assert result.output == {}
    finally:
        with admin.cursor() as cur:
            cur.execute("delete from public.tenants where id = %s", (stranger,))

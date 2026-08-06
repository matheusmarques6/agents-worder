"""The three tools the E3 adds — and the guard the E2 tools never needed.

`search_knowledge` and `get_customer_context` answer about the conversation
itself, so RLS plus the conversation id was the whole of their scope. These
answer about ORDERS, and an order belongs to a customer, so RLS alone is not a
guard at all: inside one tenant it authorises every contact to read every other
contact's parcel. The number a contact types is the number the store printed on
their receipt, and receipts are sequential.

So the claim under test is narrower than "no cross-tenant leak", and it is the
one that matters here:

    an order this contact does not own is indistinguishable, to this contact,
    from an order that does not exist.

Which is also why "not found" is a SUCCESS. The tool worked; the answer is no.
An error would tell the asker that something is there.

The connection is the one `app.py` builds — autocommit, role set, no tenant
scope — because a pre-scoped connection would hide a tool that forgot to scope
itself.
"""

import uuid
from dataclasses import dataclass

import psycopg
import pytest

from agents_runtime.tools import base as tools
from agents_runtime.tools.handoff import REASON_LIMIT, EscalateToHuman
from agents_runtime.tools.orders import GetOrder, GetTracking
from tests.db.factories import create_tenant, create_thread
from tests.db.factories_e3 import create_customer, create_order, link_contact_to_customer
from tests.support.clock import FrozenClock
from tests.support.database import as_runtime_worker
from tests.support.llm import START


@dataclass(frozen=True)
class Shopper:
    """A contact linked to a mirrored customer of a store — the arrangement
    every one of these tools walks."""

    conversation_id: uuid.UUID
    contact_id: uuid.UUID
    customer_external_id: str
    connector_account_id: uuid.UUID


@pytest.fixture
def tenant(admin: psycopg.Connection) -> uuid.UUID:
    tenant_id = create_tenant(admin)
    yield tenant_id
    with admin.cursor() as cur:
        cur.execute("delete from public.tenants where id = %s", (tenant_id,))


@pytest.fixture
def store(admin: psycopg.Connection, tenant: uuid.UUID) -> uuid.UUID:
    from tests.db.factories import create_connector_account

    return create_connector_account(admin, tenant).id


def a_shopper(
    admin: psycopg.Connection,
    tenant_id: uuid.UUID,
    connector_account_id: uuid.UUID,
    *,
    external_id: str,
) -> Shopper:
    thread = create_thread(admin, tenant_id)
    customer_id = create_customer(admin, tenant_id, connector_account_id, external_id=external_id)
    link_contact_to_customer(admin, thread.contact_id, customer_id)
    return Shopper(
        conversation_id=thread.conversation_id,
        contact_id=thread.contact_id,
        customer_external_id=external_id,
        connector_account_id=connector_account_id,
    )


def _context(tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> tools.ToolContext:
    return tools.ToolContext(tenant_id=tenant_id, conversation_id=conversation_id)


async def call(dsn: str, tool, tenant_id: uuid.UUID, conversation_id: uuid.UUID, arguments):
    async with as_runtime_worker(dsn) as conn:
        return await tools.run_tool(
            conn,
            tool,
            _context(tenant_id, conversation_id),
            arguments,
            clock=FrozenClock(START),
        )


class TestGetOrder:
    async def test_it_answers_with_the_most_recent_order_of_this_contact(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID, store: uuid.UUID
    ) -> None:
        shopper = a_shopper(admin, tenant, store, external_id="cust-1")
        create_order(
            admin,
            tenant,
            store,
            external_id="1001",
            customer_external_id="cust-1",
            platform_created_ago_seconds=86_400,
        )
        create_order(
            admin,
            tenant,
            store,
            external_id="1002",
            customer_external_id="cust-1",
            financial_status="paid",
            platform_created_ago_seconds=60,
        )

        result = await call(dsn, GetOrder(), tenant, shopper.conversation_id, {})

        assert result.success is True
        assert result.output["found"] is True
        assert result.output["order"]["order_id"] == "1002"
        assert result.output["order"]["financial_status"] == "paid"

    async def test_it_answers_the_order_the_contact_named(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID, store: uuid.UUID
    ) -> None:
        shopper = a_shopper(admin, tenant, store, external_id="cust-1")
        create_order(
            admin,
            tenant,
            store,
            external_id="1001",
            customer_external_id="cust-1",
            platform_created_ago_seconds=86_400,
        )
        create_order(
            admin,
            tenant,
            store,
            external_id="1002",
            customer_external_id="cust-1",
            platform_created_ago_seconds=60,
        )

        result = await call(dsn, GetOrder(), tenant, shopper.conversation_id, {"order_id": "1001"})

        assert result.output["order"]["order_id"] == "1001"

    async def test_an_order_of_another_contact_in_the_same_store_is_not_found(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID, store: uuid.UUID
    ) -> None:
        """The guard RLS does not give. Same tenant, same store, same table —
        and the order number is the one the other customer's receipt carries.

        A tool that scoped only by tenant would answer this, and answering it is
        an enumeration oracle: order numbers are sequential.
        """
        mine = a_shopper(admin, tenant, store, external_id="cust-1")
        a_shopper(admin, tenant, store, external_id="cust-2")
        create_order(admin, tenant, store, external_id="2001", customer_external_id="cust-2")
        create_order(admin, tenant, store, external_id="1001", customer_external_id="cust-1")

        result = await call(dsn, GetOrder(), tenant, mine.conversation_id, {"order_id": "2001"})

        assert result.success is True
        assert result.output == {"found": False}

    async def test_the_scope_never_comes_from_the_arguments(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID, store: uuid.UUID
    ) -> None:
        """The model chose these arguments after reading a stranger's message. A
        `tenant_id` among them is not a hint, it is an attack — and the only
        answer is to ignore it and scope from the job."""
        stranger = create_tenant(admin)
        try:
            from tests.db.factories import create_connector_account

            their_store = create_connector_account(admin, stranger).id
            theirs = a_shopper(admin, stranger, their_store, external_id="cust-x")
            create_order(
                admin,
                stranger,
                their_store,
                external_id="9001",
                customer_external_id="cust-x",
            )

            mine = a_shopper(admin, tenant, store, external_id="cust-1")
            create_order(admin, tenant, store, external_id="1001", customer_external_id="cust-1")

            result = await call(
                dsn,
                GetOrder(),
                tenant,
                mine.conversation_id,
                {"order_id": "9001", "tenant_id": str(stranger)},
            )

            assert result.output == {"found": False}
            assert theirs.customer_external_id == "cust-x"
        finally:
            with admin.cursor() as cur:
                cur.execute("delete from public.tenants where id = %s", (stranger,))

    async def test_it_refuses_a_conversation_of_another_tenant(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """A conversation that is not ours is a refusal, not an empty answer:
        "no orders" would read to the model as a customer who never bought."""
        stranger = create_tenant(admin)
        try:
            theirs = create_thread(admin, stranger)

            result = await call(dsn, GetOrder(), tenant, theirs.conversation_id, {})

            assert result.success is False
            assert result.output == {}
        finally:
            with admin.cursor() as cur:
                cur.execute("delete from public.tenants where id = %s", (stranger,))

    async def test_a_contact_never_linked_to_a_customer_simply_has_no_orders(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID, store: uuid.UUID
    ) -> None:
        """`contacts.customer_id` is filled by the mirror, and until it is, the
        truthful answer is "no orders here" — not an error, and not somebody
        else's order because the join fell through."""
        thread = create_thread(admin, tenant)
        create_order(admin, tenant, store, external_id="1001", customer_external_id="cust-1")

        result = await call(dsn, GetOrder(), tenant, thread.conversation_id, {})

        assert result.success is True
        assert result.output == {"found": False}

    async def test_an_order_id_that_is_not_text_is_refused(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID, store: uuid.UUID
    ) -> None:
        """Strict parsing, the webhook doctrine. Coercing 42 to "42" would turn a
        malformed call into a plausible lookup."""
        shopper = a_shopper(admin, tenant, store, external_id="cust-1")

        result = await call(dsn, GetOrder(), tenant, shopper.conversation_id, {"order_id": 42})

        assert result.success is False
        assert "order_id" in result.error

    async def test_money_reaches_the_model_as_text(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID, store: uuid.UUID
    ) -> None:
        """The output is stored as jsonb and read by a model. Through a float,
        199.90 becomes 199.89999999999998 in both places."""
        shopper = a_shopper(admin, tenant, store, external_id="cust-1")
        create_order(
            admin,
            tenant,
            store,
            external_id="1001",
            customer_external_id="cust-1",
            total="199.90",
        )

        result = await call(dsn, GetOrder(), tenant, shopper.conversation_id, {})

        assert result.output["order"]["total"] == "199.90"
        assert result.output["order"]["currency"] == "BRL"

    async def test_the_execution_is_on_the_record(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID, store: uuid.UUID
    ) -> None:
        shopper = a_shopper(admin, tenant, store, external_id="cust-1")
        create_order(admin, tenant, store, external_id="1001", customer_external_id="cust-1")

        await call(dsn, GetOrder(), tenant, shopper.conversation_id, {"order_id": "1001"})

        with admin.cursor() as cur:
            cur.execute(
                """
                select tool_name, input, success, tenant_id
                  from internal.tool_calls where conversation_id = %s
                """,
                (shopper.conversation_id,),
            )
            (row,) = cur.fetchall()

        assert row[0] == "get_order"
        assert row[1] == {"order_id": "1001"}
        assert row[2] is True
        assert row[3] == tenant


class TestGetTracking:
    async def test_it_reads_the_code_the_mirror_holds(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID, store: uuid.UUID
    ) -> None:
        shopper = a_shopper(admin, tenant, store, external_id="cust-1")
        create_order(
            admin,
            tenant,
            store,
            external_id="1001",
            customer_external_id="cust-1",
            tracking_code="BR123456789BR",
            tracking_status="em trânsito",
        )

        result = await call(dsn, GetTracking(), tenant, shopper.conversation_id, {})

        assert result.success is True
        assert result.output["found"] is True
        assert result.output["order_id"] == "1001"
        assert result.output["tracking_code"] == "BR123456789BR"
        assert result.output["tracking_status"] == "em trânsito"

    async def test_an_order_without_a_code_says_so_instead_of_inventing_one(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID, store: uuid.UUID
    ) -> None:
        """ "We have your order and it has not shipped" and "we have no idea who
        you are" are different answers, and the mirror is the only source — a
        tracking API is pendência nº 3, not this step."""
        shopper = a_shopper(admin, tenant, store, external_id="cust-1")
        create_order(admin, tenant, store, external_id="1001", customer_external_id="cust-1")

        result = await call(dsn, GetTracking(), tenant, shopper.conversation_id, {})

        assert result.output["found"] is True
        assert result.output["tracking_code"] is None
        assert result.output["tracking_status"] is None

    async def test_the_tracking_of_another_contacts_order_is_not_found(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID, store: uuid.UUID
    ) -> None:
        """A parcel's whereabouts is the same secret as the order it belongs to."""
        mine = a_shopper(admin, tenant, store, external_id="cust-1")
        a_shopper(admin, tenant, store, external_id="cust-2")
        create_order(
            admin,
            tenant,
            store,
            external_id="2001",
            customer_external_id="cust-2",
            tracking_code="BR999999999BR",
        )

        result = await call(dsn, GetTracking(), tenant, mine.conversation_id, {"order_id": "2001"})

        assert result.output == {"found": False}

    async def test_it_refuses_a_conversation_of_another_tenant(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        stranger = create_tenant(admin)
        try:
            theirs = create_thread(admin, stranger)

            result = await call(dsn, GetTracking(), tenant, theirs.conversation_id, {})

            assert result.success is False
        finally:
            with admin.cursor() as cur:
                cur.execute("delete from public.tenants where id = %s", (stranger,))


class TestEscalateToHuman:
    async def test_it_puts_the_conversation_in_the_queue_for_a_person(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        thread = create_thread(admin, tenant)

        result = await call(
            dsn,
            EscalateToHuman(),
            tenant,
            thread.conversation_id,
            {"reason": "cliente pede reembolso fora da política"},
        )

        assert result.success is True
        assert result.output == {"escalated": True, "already": False}

        with admin.cursor() as cur:
            cur.execute(
                """
                select state, takeover_user_id, takeover_at
                  from public.conversations where id = %s
                """,
                (thread.conversation_id,),
            )
            state, user_id, at = cur.fetchone()
            cur.execute(
                "select type, severity, status, payload from public.alerts where tenant_id = %s",
                (tenant,),
            )
            (alert,) = cur.fetchall()

        assert state == "humano"
        # Waiting for a person is not the same as a person having taken it — the
        # inbox of E5 has to be able to tell those apart.
        assert user_id is None and at is None
        assert alert[0] == "handoff"
        assert alert[2] == "open"
        assert alert[3]["reason"] == "cliente pede reembolso fora da política"
        assert alert[3]["conversation_id"] == str(thread.conversation_id)

    async def test_escalating_twice_is_not_a_failure(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """A failure here would push the model into apologising for a handover it
        already made — the shape `record_optout` settled."""
        thread = create_thread(admin, tenant)
        await call(dsn, EscalateToHuman(), tenant, thread.conversation_id, {"reason": "primeira"})

        result = await call(
            dsn, EscalateToHuman(), tenant, thread.conversation_id, {"reason": "segunda"}
        )

        assert result.success is True
        assert result.output == {"escalated": True, "already": True}

        with admin.cursor() as cur:
            cur.execute("select count(*) from public.alerts where tenant_id = %s", (tenant,))
            (alerts,) = cur.fetchone()
        # One conversation waiting is one thing for a human to do. A second
        # alert would be the same customer queued twice.
        assert alerts == 1

    async def test_it_never_escalates_a_conversation_of_another_tenant(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        stranger = create_tenant(admin)
        try:
            theirs = create_thread(admin, stranger)

            result = await call(
                dsn, EscalateToHuman(), tenant, theirs.conversation_id, {"reason": "oi"}
            )

            assert result.success is False
            with admin.cursor() as cur:
                cur.execute(
                    "select state from public.conversations where id = %s",
                    (theirs.conversation_id,),
                )
                (state,) = cur.fetchone()
                cur.execute("select count(*) from public.alerts")
                (alerts,) = cur.fetchone()

            assert state == "ia"
            assert alerts == 0
        finally:
            with admin.cursor() as cur:
                cur.execute("delete from public.tenants where id = %s", (stranger,))

    async def test_the_note_is_bounded(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """The note is text a contact's message influenced. It is stored and
        shown, never interpreted — and a model that emits a novel must not become
        a payload nobody can open."""
        thread = create_thread(admin, tenant)

        await call(
            dsn,
            EscalateToHuman(),
            tenant,
            thread.conversation_id,
            {"reason": "x" * 5_000},
        )

        with admin.cursor() as cur:
            cur.execute("select payload from public.alerts where tenant_id = %s", (tenant,))
            (payload,) = cur.fetchone()

        assert len(payload["reason"]) == REASON_LIMIT

    async def test_an_escalation_without_a_reason_is_refused(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """The note is the whole value of the handover to the person who picks it
        up. An escalation with nothing written on it is a conversation somebody
        has to read from the top."""
        thread = create_thread(admin, tenant)

        result = await call(dsn, EscalateToHuman(), tenant, thread.conversation_id, {})

        assert result.success is False
        assert "reason" in result.error

        with admin.cursor() as cur:
            cur.execute(
                "select state from public.conversations where id = %s",
                (thread.conversation_id,),
            )
            (state,) = cur.fetchone()
        assert state == "ia"

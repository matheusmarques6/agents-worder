"""The leak suite for the conversation tables — E1 · PR-0.

Same shape and the same teeth as the identity suite of E0-07: two tenants,
three credentials, and **any row returned or affected fails**.

What is asserted per credential is not symmetric, and that is deliberate. The
sender has no grant on `contacts`, `conversations` or `messages`, so pointing it
at them would produce "permission denied" — which is evidence about a GRANT,
not about a policy (decisão 16 do E0-07). The sender is attacked where it does
have privilege: the outbox.
"""

import uuid

import psycopg
import pytest

from tests.db.conftest import TwoTenants, as_app_role, as_authenticated_user
from tests.db.factories import Thread, create_message, create_outbox_item, create_thread

pytestmark = pytest.mark.rls


@pytest.fixture
def threads(admin: psycopg.Connection, two_tenants: TwoTenants) -> dict[str, Thread]:
    """One full conversation per tenant, each with a message and a queued send."""
    built = {}
    for label, tenant in (("a", two_tenants.a), ("b", two_tenants.b)):
        thread = create_thread(admin, tenant.id)
        create_message(admin, tenant.id, thread, seq=1)
        create_outbox_item(admin, tenant.id, thread)
        built[label] = thread
    return built


class TestReadIsConfinedToOneTenant:
    def test_worker_of_a_cannot_read_the_conversation_of_b(
        self, dsn: str, two_tenants: TwoTenants, threads: dict[str, Thread]
    ) -> None:
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            rows = conn.execute(
                "select id from public.conversations where id = %s",
                (threads["b"].conversation_id,),
            ).fetchall()

        assert rows == []

    def test_worker_of_a_cannot_read_the_messages_of_b(
        self, dsn: str, two_tenants: TwoTenants, threads: dict[str, Thread]
    ) -> None:
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            rows = conn.execute(
                "select id from public.messages where conversation_id = %s",
                (threads["b"].conversation_id,),
            ).fetchall()

        assert rows == []

    def test_worker_of_a_cannot_read_the_contacts_of_b(
        self, dsn: str, two_tenants: TwoTenants, threads: dict[str, Thread]
    ) -> None:
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            rows = conn.execute(
                "select id from public.contacts where id = %s",
                (threads["b"].contact_id,),
            ).fetchall()

        assert rows == []

    def test_sender_of_a_cannot_read_the_outbox_of_b(
        self, dsn: str, two_tenants: TwoTenants, threads: dict[str, Thread]
    ) -> None:
        with as_app_role(dsn, "sender_role", two_tenants.a.id) as conn:
            rows = conn.execute(
                "select id from internal.message_outbox where tenant_id = %s",
                (two_tenants.b.id,),
            ).fetchall()

        assert rows == []

    def test_user_of_a_cannot_read_the_conversation_of_b(
        self, dsn: str, two_tenants: TwoTenants, threads: dict[str, Thread]
    ) -> None:
        with as_authenticated_user(dsn, two_tenants.a.user_id) as conn:
            rows = conn.execute(
                "select id from public.conversations where id = %s",
                (threads["b"].conversation_id,),
            ).fetchall()

        assert rows == []

    def test_user_of_a_cannot_read_the_messages_of_b(
        self, dsn: str, two_tenants: TwoTenants, threads: dict[str, Thread]
    ) -> None:
        with as_authenticated_user(dsn, two_tenants.a.user_id) as conn:
            rows = conn.execute(
                "select id from public.messages where conversation_id = %s",
                (threads["b"].conversation_id,),
            ).fetchall()

        assert rows == []


class TestWriteIsConfinedToOneTenant:
    def test_worker_of_a_cannot_move_the_conversation_of_b(
        self, dsn: str, two_tenants: TwoTenants, threads: dict[str, Thread]
    ) -> None:
        # The write direction of a leak, and the one a SELECT-only suite misses:
        # advancing another tenant's `last_processed_seq` would make their agent
        # skip a customer's message.
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            affected = conn.execute(
                "update public.conversations set last_processed_seq = 99 where id = %s",
                (threads["b"].conversation_id,),
            ).rowcount

        assert affected == 0

    def test_worker_of_a_cannot_write_a_message_into_the_conversation_of_b(
        self, dsn: str, two_tenants: TwoTenants, threads: dict[str, Thread]
    ) -> None:
        # WITH CHECK, not USING: putting words in another tenant's thread is the
        # leak that reads as "no rows returned" in every read-only test.
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    """
                    insert into public.messages
                        (tenant_id, conversation_id, direction, seq, channel,
                         author_type, content)
                    values (%s, %s, 'outbound', 99, 'whatsapp_cloud', 'agent', '{}'::jsonb)
                    """,
                    (two_tenants.b.id, threads["b"].conversation_id),
                )

    def test_sender_of_a_cannot_mark_the_send_of_b_as_sent(
        self, dsn: str, two_tenants: TwoTenants, threads: dict[str, Thread]
    ) -> None:
        with as_app_role(dsn, "sender_role", two_tenants.a.id) as conn:
            affected = conn.execute(
                "update internal.message_outbox set status = 'sent' where tenant_id = %s",
                (two_tenants.b.id,),
            ).rowcount

        assert affected == 0


class TestTheLegitimatePathStillWorks:
    def test_worker_reads_its_own_conversation(
        self, dsn: str, two_tenants: TwoTenants, threads: dict[str, Thread]
    ) -> None:
        # Without this, a policy of `using (false)` would pass every test above.
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            rows = conn.execute(
                "select id from public.conversations where id = %s",
                (threads["a"].conversation_id,),
            ).fetchall()

        assert [row[0] for row in rows] == [threads["a"].conversation_id]

    def test_sender_reads_its_own_outbox(
        self, dsn: str, two_tenants: TwoTenants, threads: dict[str, Thread]
    ) -> None:
        with as_app_role(dsn, "sender_role", two_tenants.a.id) as conn:
            rows = conn.execute(
                "select id from internal.message_outbox where tenant_id = %s",
                (two_tenants.a.id,),
            ).fetchall()

        assert len(rows) == 1


class TestTenantScopeCannotComeFromTheClient:
    @pytest.mark.parametrize("scope", ["", "not-a-uuid"])
    def test_an_unusable_tenant_scope_reads_nothing(
        self, dsn: str, threads: dict[str, Thread], scope: str
    ) -> None:
        # Unset or garbage must read NOTHING, never everything. `tenant_id = NULL`
        # is never true, which is what makes failing closed the default.
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("select set_config('app.tenant_id', %s, true)", (scope,))
                cur.execute("set role worker_role")
                rows = cur.execute("select id from public.conversations").fetchall()

        assert rows == []

    def test_a_tenant_id_in_the_row_does_not_grant_access(
        self, dsn: str, two_tenants: TwoTenants, threads: dict[str, Thread]
    ) -> None:
        # The scope comes from `SET LOCAL app.tenant_id`, never from the data
        # being asked about: naming tenant B in the WHERE clause is exactly the
        # attack the policy exists to defeat.
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            rows = conn.execute(
                "select id from public.conversations where tenant_id = %s",
                (two_tenants.b.id,),
            ).fetchall()

        assert rows == []


class TestUnknownTenantIsNotAKey:
    def test_a_worker_scoped_to_a_random_uuid_reads_nothing(
        self, dsn: str, threads: dict[str, Thread]
    ) -> None:
        with as_app_role(dsn, "worker_role", uuid.uuid4()) as conn:
            rows = conn.execute("select id from public.conversations").fetchall()

        assert rows == []

"""The cross-tenant leak suite for the E3 tables — S2.

Same shape as the E0-07 and E2 suites, on the seven tables this milestone adds:
three credentials reach the data — the user's JWT, `worker_role` and
`sender_role` — and none of them may see one row belonging to the other tenant.
**Any row returned fails the suite.**

Two of these tables are worse to leak than anything the earlier milestones
added, which is why the write half of the suite is longer here:

· `suppression_list` is the only record of somebody saying "do not message me".
  A worker of tenant A that could delete a row of tenant B would turn a
  compliance record into an outage of the LGPD promise, and the merchant would
  never see it — the touch would simply go out.

· `orders` and `customers` are the mirror of somebody else's business. A read
  across the boundary is not a bug, it is a competitor reading revenue.

The tables are all merchant-facing and live in `public`: the merchant's own hub
shows funnels, suppressions, orders and recovered revenue (E5). The trail of
`audit_log` is here too, and it is the one whose platform rows (`tenant_id is
null`) must be invisible to every tenant — the same shape `alerts` already has.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import psycopg
import pytest

from tests.db.conftest import TwoTenants, as_app_role, as_authenticated_user
from tests.db.factories import create_connector_account, create_thread
from tests.db.factories_e3 import (
    create_audit_entry,
    create_customer,
    create_funnel,
    create_funnel_conversion,
    create_order,
    create_scheduled_touch,
    create_suppression,
)

pytestmark = pytest.mark.rls

E3_TABLES = (
    "public.funnels",
    "public.scheduled_touches",
    "public.suppression_list",
    "public.orders",
    "public.customers",
    "public.funnel_conversions",
    "public.audit_log",
)


@dataclass(frozen=True)
class TenantRows:
    funnel_id: uuid.UUID
    contact_id: uuid.UUID
    touch_id: uuid.UUID
    order_id: uuid.UUID
    customer_id: uuid.UUID


@pytest.fixture
def e3_rows(admin: psycopg.Connection, two_tenants: TwoTenants) -> Iterator[dict]:
    """One row of every E3 table, in BOTH tenants.

    Populating only the victim would make a leak look like an empty table.
    """
    rows = {}
    for label, tenant in (("a", two_tenants.a), ("b", two_tenants.b)):
        account = create_connector_account(admin, tenant.id)
        thread = create_thread(admin, tenant.id)
        funnel = create_funnel(admin, tenant.id)
        order_id = create_order(admin, tenant.id, account.id)
        customer_id = create_customer(admin, tenant.id, account.id)
        touch_id = create_scheduled_touch(
            admin,
            tenant.id,
            funnel.id,
            thread.contact_id,
            conversation_id=thread.conversation_id,
            order_id=order_id,
        )
        create_suppression(admin, tenant.id, thread.contact_id)
        create_funnel_conversion(
            admin,
            tenant.id,
            funnel_id=funnel.id,
            contact_id=thread.contact_id,
            touch_id=touch_id,
            order_id=order_id,
        )
        create_audit_entry(admin, tenant.id, action="agent.approve")
        rows[label] = TenantRows(
            funnel_id=funnel.id,
            contact_id=thread.contact_id,
            touch_id=touch_id,
            order_id=order_id,
            customer_id=customer_id,
        )

    yield rows


def rows_of_the_other_tenant(conn: psycopg.Connection, table: str, tenant_id: uuid.UUID) -> list:
    """What this credential manages to read of another tenant. Should be nothing.

    A denial by privilege counts as nothing: the row was not reached either way.
    """
    try:
        return conn.execute(
            f"select id from {table} where tenant_id = %s", (tenant_id,)
        ).fetchall()
    except psycopg.errors.InsufficientPrivilege:
        return []


class TestReadIsConfinedToOneTenant:
    @pytest.mark.parametrize("table", E3_TABLES)
    @pytest.mark.parametrize("role", ["worker_role", "sender_role"])
    def test_an_app_role_of_a_cannot_read_the_rows_of_b(
        self, dsn: str, two_tenants: TwoTenants, e3_rows: dict, table: str, role: str
    ) -> None:
        with as_app_role(dsn, role, two_tenants.a.id) as conn:
            leaked = rows_of_the_other_tenant(conn, table, two_tenants.b.id)

        assert leaked == []

    @pytest.mark.parametrize("table", E3_TABLES)
    def test_the_user_of_a_cannot_read_the_rows_of_b(
        self, dsn: str, two_tenants: TwoTenants, e3_rows: dict, table: str
    ) -> None:
        with as_authenticated_user(dsn, two_tenants.a.user_id) as conn:
            leaked = rows_of_the_other_tenant(conn, table, two_tenants.b.id)

        assert leaked == []


class TestWriteIsConfinedToOneTenant:
    def test_the_worker_of_a_cannot_delete_a_suppression_of_b(
        self, dsn: str, two_tenants: TwoTenants, e3_rows: dict
    ) -> None:
        # The nastiest write in this milestone: erasing somebody else's "do not
        # message me". It is silent — the next touch simply goes out.
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            affected = conn.execute(
                "delete from public.suppression_list where tenant_id = %s",
                (two_tenants.b.id,),
            ).rowcount
            conn.commit()

        assert affected == 0

    def test_the_worker_of_a_cannot_cancel_a_touch_of_b(
        self, dsn: str, two_tenants: TwoTenants, e3_rows: dict
    ) -> None:
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            affected = conn.execute(
                """
                update public.scheduled_touches
                   set status = 'cancelled', cancel_reason = 'manual'
                 where tenant_id = %s
                """,
                (two_tenants.b.id,),
            ).rowcount
            conn.commit()

        assert affected == 0

    def test_the_worker_of_a_cannot_mark_an_order_of_b_as_paid(
        self, dsn: str, two_tenants: TwoTenants, e3_rows: dict
    ) -> None:
        # Paying somebody else's order would cancel their funnel and credit
        # revenue that never arrived.
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            affected = conn.execute(
                "update public.orders set financial_status = 'paid' where tenant_id = %s",
                (two_tenants.b.id,),
            ).rowcount
            conn.commit()

        assert affected == 0

    def test_the_worker_of_a_cannot_insert_a_suppression_into_b(
        self, dsn: str, two_tenants: TwoTenants, e3_rows: dict
    ) -> None:
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            with pytest.raises(
                (psycopg.errors.InsufficientPrivilege, psycopg.errors.CheckViolation)
            ):
                create_suppression(conn, two_tenants.b.id, e3_rows["b"].contact_id)

    def test_the_worker_of_a_cannot_credit_revenue_to_b(
        self, dsn: str, two_tenants: TwoTenants, e3_rows: dict
    ) -> None:
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            with pytest.raises(
                (psycopg.errors.InsufficientPrivilege, psycopg.errors.CheckViolation)
            ):
                create_funnel_conversion(
                    conn, two_tenants.b.id, funnel_id=e3_rows["b"].funnel_id
                )

    def test_the_hub_user_of_a_cannot_write_a_funnel_at_all(
        self, dsn: str, two_tenants: TwoTenants, e3_rows: dict
    ) -> None:
        # The funnel screens are E5. Until they exist the Data API role reads
        # and nothing else — an unused write grant is a leak waiting for a bug.
        with as_authenticated_user(dsn, two_tenants.a.user_id) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "update public.funnels set enabled = false where tenant_id = %s",
                    (two_tenants.a.id,),
                )


class TestTheLegitimatePathStillWorks:
    """A boundary that also blocks the owner is not security, it is an outage."""

    def test_the_worker_reads_its_own_due_touches(
        self, dsn: str, two_tenants: TwoTenants, e3_rows: dict
    ) -> None:
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            rows = conn.execute("select id from public.scheduled_touches").fetchall()

        assert [row[0] for row in rows] == [e3_rows["a"].touch_id]

    def test_the_worker_reads_its_own_suppression_list(
        self, dsn: str, two_tenants: TwoTenants, e3_rows: dict
    ) -> None:
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            rows = conn.execute(
                "select contact_id from public.suppression_list"
            ).fetchall()

        assert [row[0] for row in rows] == [e3_rows["a"].contact_id]

    def test_the_worker_cancels_its_own_touch(
        self, dsn: str, two_tenants: TwoTenants, e3_rows: dict
    ) -> None:
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            affected = conn.execute(
                """
                update public.scheduled_touches
                   set status = 'cancelled', cancel_reason = 'stale_order_paid'
                 where id = %s
                """,
                (e3_rows["a"].touch_id,),
            ).rowcount
            conn.commit()

        assert affected == 1

    def test_the_user_reads_the_orders_of_their_own_tenant(
        self, dsn: str, two_tenants: TwoTenants, e3_rows: dict
    ) -> None:
        with as_authenticated_user(dsn, two_tenants.a.user_id) as conn:
            rows = conn.execute("select id from public.orders").fetchall()

        assert [row[0] for row in rows] == [e3_rows["a"].order_id]

    def test_the_user_reads_the_recovered_revenue_of_their_own_tenant(
        self, dsn: str, two_tenants: TwoTenants, e3_rows: dict
    ) -> None:
        with as_authenticated_user(dsn, two_tenants.a.user_id) as conn:
            rows = conn.execute(
                "select amount from public.funnel_conversions"
            ).fetchall()

        assert len(rows) == 1


class TestAPlatformAuditEntryBelongsToNobody:
    def test_no_tenant_sees_the_platform_rows(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # `tenant_id is null` means the platform did it. The policies scope on
        # equality and `tenant_id = null` is never true, so those rows are
        # invisible to every tenant — the same shape `alerts` already has.
        create_audit_entry(admin, None, action="platform.migration")

        with as_authenticated_user(dsn, two_tenants.a.user_id) as conn:
            rows = conn.execute(
                "select id from public.audit_log where tenant_id is null"
            ).fetchall()

        assert rows == []


class TestTheRolesCannotOptOut:
    @pytest.mark.parametrize("table", E3_TABLES)
    def test_row_level_security_is_enabled(self, admin: psycopg.Connection, table: str) -> None:
        schema, name = table.split(".")
        row = admin.execute(
            """
            select c.relrowsecurity
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = %s and c.relname = %s
            """,
            (schema, name),
        ).fetchone()

        assert row is not None, f"{table} does not exist"
        assert row[0] is True

    @pytest.mark.parametrize("table", E3_TABLES)
    @pytest.mark.parametrize("role", ["worker_role", "sender_role"])
    def test_no_app_role_owns_an_e3_table(
        self, admin: psycopg.Connection, table: str, role: str
    ) -> None:
        schema, name = table.split(".")
        owner = admin.execute(
            "select tableowner from pg_tables where schemaname = %s and tablename = %s",
            (schema, name),
        ).fetchone()

        assert owner is not None, f"{table} does not exist"
        assert owner[0] != role


class TestTenantIdCannotComeFromTheClient:
    @pytest.mark.parametrize("table", E3_TABLES)
    def test_an_unset_tenant_scope_reads_nothing(
        self, dsn: str, e3_rows: dict, table: str
    ) -> None:
        """Fail closed. A pool that forgot to scope the unit of work sees zero rows."""
        with psycopg.connect(dsn) as conn:
            conn.execute("set role worker_role")
            try:
                rows = conn.execute(f"select id from {table}").fetchall()
            except psycopg.errors.InsufficientPrivilege:
                rows = []

        assert rows == []

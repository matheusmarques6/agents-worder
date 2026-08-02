"""The cross-tenant leak suite — E0-07.

This is the most important gate in the project, and it is blocking on every PR
(core/testes-e-cicd.md §3.2). The shape it asserts is the one from the
architecture: three credentials reach the data — the user's JWT, `worker_role`
and `sender_role` — and every one of them must be confined to its own tenant.
**Any row returned or affected fails the suite.**

`tenant_id` never comes from the client. Here that is literal: the app roles
carry it in `app.tenant_id`, the user carries it implicitly through
`memberships`, and neither can be talked into a different value.
"""

import uuid

import psycopg
import pytest

from tests.db.conftest import TwoTenants, as_app_role, as_authenticated_user

pytestmark = pytest.mark.rls


class TestReadIsConfinedToOneTenant:
    def test_worker_role_of_a_cannot_read_memberships_of_b(
        self, dsn: str, two_tenants: TwoTenants
    ) -> None:
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            rows = conn.execute(
                "select id from public.memberships where tenant_id = %s",
                (two_tenants.b.id,),
            ).fetchall()

        assert rows == []

    def test_sender_role_of_a_cannot_read_memberships_of_b(
        self, dsn: str, two_tenants: TwoTenants
    ) -> None:
        with as_app_role(dsn, "sender_role", two_tenants.a.id) as conn:
            rows = conn.execute(
                "select id from public.memberships where tenant_id = %s",
                (two_tenants.b.id,),
            ).fetchall()

        assert rows == []

    def test_user_of_a_cannot_read_memberships_of_b(
        self, dsn: str, two_tenants: TwoTenants
    ) -> None:
        with as_authenticated_user(dsn, two_tenants.a.user_id) as conn:
            rows = conn.execute(
                "select id from public.memberships where tenant_id = %s",
                (two_tenants.b.id,),
            ).fetchall()

        assert rows == []

    def test_worker_role_of_a_cannot_read_tenant_b(self, dsn: str, two_tenants: TwoTenants) -> None:
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            rows = conn.execute(
                "select id from public.tenants where id = %s", (two_tenants.b.id,)
            ).fetchall()

        assert rows == []

    def test_user_of_a_cannot_read_tenant_b(self, dsn: str, two_tenants: TwoTenants) -> None:
        with as_authenticated_user(dsn, two_tenants.a.user_id) as conn:
            rows = conn.execute(
                "select id from public.tenants where id = %s", (two_tenants.b.id,)
            ).fetchall()

        assert rows == []

    def test_user_of_a_cannot_read_the_profile_of_b(
        self, dsn: str, two_tenants: TwoTenants
    ) -> None:
        with as_authenticated_user(dsn, two_tenants.a.user_id) as conn:
            rows = conn.execute(
                "select user_id from public.profiles where user_id = %s",
                (two_tenants.b.user_id,),
            ).fetchall()

        assert rows == []


class TestTheLegitimatePathStillWorks:
    """A boundary that also blocks the owner is not security, it is an outage."""

    def test_worker_role_reads_its_own_tenants_memberships(
        self, dsn: str, two_tenants: TwoTenants
    ) -> None:
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            rows = conn.execute(
                "select id from public.memberships where tenant_id = %s",
                (two_tenants.a.id,),
            ).fetchall()

        assert [row[0] for row in rows] == [two_tenants.a.membership_id]

    def test_user_reads_their_own_membership(self, dsn: str, two_tenants: TwoTenants) -> None:
        with as_authenticated_user(dsn, two_tenants.a.user_id) as conn:
            rows = conn.execute("select id from public.memberships").fetchall()

        assert [row[0] for row in rows] == [two_tenants.a.membership_id]


class TestWriteIsConfinedToOneTenant:
    def test_worker_role_of_a_cannot_delete_memberships_of_b(
        self, dsn: str, two_tenants: TwoTenants
    ) -> None:
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            affected = conn.execute(
                "delete from public.memberships where tenant_id = %s",
                (two_tenants.b.id,),
            ).rowcount
            conn.commit()

        assert affected == 0

    def test_worker_role_of_a_cannot_update_memberships_of_b(
        self, dsn: str, two_tenants: TwoTenants
    ) -> None:
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            affected = conn.execute(
                "update public.memberships set role = 'attendant' where tenant_id = %s",
                (two_tenants.b.id,),
            ).rowcount
            conn.commit()

        assert affected == 0

    def test_worker_role_of_a_cannot_insert_a_membership_into_b(
        self, dsn: str, two_tenants: TwoTenants
    ) -> None:
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    """
                    insert into public.memberships (tenant_id, user_id, role)
                    values (%s, %s, 'attendant')
                    """,
                    (two_tenants.b.id, two_tenants.a.user_id),
                )


class TestTheRolesCannotOptOut:
    """RLS only binds a role that has no way around it.

    Superuser, BYPASSRLS and table ownership each silently disable every policy
    above. Without these three assertions the whole suite could be green against
    a role for which RLS never applied.
    """

    @pytest.mark.parametrize("role", ["worker_role", "sender_role"])
    def test_role_has_no_bypassrls_and_is_not_superuser(
        self, admin: psycopg.Connection, role: str
    ) -> None:
        row = admin.execute(
            "select rolbypassrls, rolsuper from pg_roles where rolname = %s", (role,)
        ).fetchone()

        assert row is not None, f"{role} does not exist"
        assert row == (False, False)

    @pytest.mark.parametrize("role", ["worker_role", "sender_role"])
    @pytest.mark.parametrize("table", ["tenants", "profiles", "memberships"])
    def test_role_does_not_own_the_protected_tables(
        self, admin: psycopg.Connection, role: str, table: str
    ) -> None:
        owner = admin.execute(
            "select tableowner from pg_tables where schemaname = 'public' and tablename = %s",
            (table,),
        ).fetchone()

        assert owner is not None, f"public.{table} does not exist"
        assert owner[0] != role

    @pytest.mark.parametrize("table", ["tenants", "profiles", "memberships"])
    def test_row_level_security_is_enabled(self, admin: psycopg.Connection, table: str) -> None:
        row = admin.execute(
            """
            select c.relrowsecurity
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'public' and c.relname = %s
            """,
            (table,),
        ).fetchone()

        assert row is not None, f"public.{table} does not exist"
        assert row[0] is True


class TestTenantIdCannotComeFromTheClient:
    def test_an_unset_tenant_scope_reads_nothing(self, dsn: str, two_tenants: TwoTenants) -> None:
        """Fail closed. A pool that forgot to scope the unit of work sees zero rows."""
        with psycopg.connect(dsn) as conn:
            conn.execute("set role worker_role")
            rows = conn.execute("select id from public.memberships").fetchall()

        assert rows == []

    def test_a_garbage_tenant_scope_reads_nothing(self, dsn: str, two_tenants: TwoTenants) -> None:
        with as_app_role(dsn, "worker_role", uuid.uuid4()) as conn:
            rows = conn.execute("select id from public.memberships").fetchall()

        assert rows == []

    def test_a_malformed_tenant_scope_reads_nothing(
        self, dsn: str, two_tenants: TwoTenants
    ) -> None:
        """Not a uuid at all. Failing closed means zero rows, not an error on
        every statement — a pool that writes garbage must not turn into an
        outage of its whole connection."""
        with psycopg.connect(dsn) as conn:
            conn.execute("select set_config('app.tenant_id', 'not-a-uuid', true)")
            conn.execute("set role worker_role")
            rows = conn.execute("select id from public.memberships").fetchall()

        assert rows == []

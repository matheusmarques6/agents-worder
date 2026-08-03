"""The cross-tenant leak suite for the E2 tables — S2.

Same shape as the E0-07 suite, on the tables this milestone adds: three
credentials reach the data — the user's JWT, `worker_role` and `sender_role` —
and none of them may see one row belonging to the other tenant. **Any row
returned fails the suite.**

Two things are new here and both are deliberate:

· A credential can be stopped by a policy (zero rows) or by never having been
  granted the table at all (`InsufficientPrivilege`). Both are "did not reach
  it", and the helper accepts either — asserting only on the empty result would
  turn a missing GRANT into a false green, and asserting only on the exception
  would force grants the roles have no business holding.

· The evaluation tables are internal (ADR-11 / CLAUDE.md: outbox, pgmq queues
  and evals stay out of the Data API schema). So besides the tenant boundary,
  the Data API roles must not reach them at all.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import psycopg
import pytest

from tests.db.conftest import TwoTenants, as_app_role, as_authenticated_user
from tests.db.factories import (
    create_agent_version,
    create_alert,
    create_eval_run,
    create_judge_score,
    create_knowledge_chunk,
    create_llm_call,
    create_scenario,
    create_thread,
    create_tool_call,
)

pytestmark = pytest.mark.rls

# Every E2 table that carries tenant_id. The merchant-facing ones live in
# `public` behind RLS; the evaluation ones live in `internal` and never leave it.
MERCHANT_TABLES = ("public.agent_versions", "public.knowledge_chunks", "public.alerts")
INTERNAL_TABLES = (
    "internal.scenarios",
    "internal.eval_runs",
    "internal.judge_scores",
    "internal.tool_calls",
    "internal.llm_calls",
)
E2_TABLES = MERCHANT_TABLES + INTERNAL_TABLES

DATA_API_ROLES = ("anon", "authenticated", "service_role")


@dataclass(frozen=True)
class TenantRows:
    agent_version_id: uuid.UUID


@pytest.fixture
def e2_rows(admin: psycopg.Connection, two_tenants: TwoTenants) -> Iterator[dict]:
    """One row of every E2 table, in BOTH tenants.

    Populating only the victim would make a leak look like an empty table.
    """
    rows = {}
    for label, tenant in (("a", two_tenants.a), ("b", two_tenants.b)):
        version_id = create_agent_version(admin, tenant.id, status="active")
        thread = create_thread(admin, tenant.id)
        create_knowledge_chunk(admin, tenant.id)
        create_alert(admin, tenant.id)
        create_scenario(admin, tenant.id)
        create_eval_run(admin, tenant.id, version_id)
        create_judge_score(admin, tenant.id, conversation_id=thread.conversation_id)
        create_tool_call(admin, tenant.id, thread)
        create_llm_call(admin, tenant.id)
        rows[label] = TenantRows(agent_version_id=version_id)

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
    @pytest.mark.parametrize("table", E2_TABLES)
    @pytest.mark.parametrize("role", ["worker_role", "sender_role"])
    def test_an_app_role_of_a_cannot_read_the_rows_of_b(
        self, dsn: str, two_tenants: TwoTenants, e2_rows: dict, table: str, role: str
    ) -> None:
        with as_app_role(dsn, role, two_tenants.a.id) as conn:
            leaked = rows_of_the_other_tenant(conn, table, two_tenants.b.id)

        assert leaked == []

    @pytest.mark.parametrize("table", E2_TABLES)
    def test_the_user_of_a_cannot_read_the_rows_of_b(
        self, dsn: str, two_tenants: TwoTenants, e2_rows: dict, table: str
    ) -> None:
        with as_authenticated_user(dsn, two_tenants.a.user_id) as conn:
            leaked = rows_of_the_other_tenant(conn, table, two_tenants.b.id)

        assert leaked == []


class TestWriteIsConfinedToOneTenant:
    def test_the_worker_of_a_cannot_activate_a_version_of_b(
        self, dsn: str, two_tenants: TwoTenants, e2_rows: dict
    ) -> None:
        # The nastiest write in this milestone: flipping another tenant's agent.
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            affected = conn.execute(
                "update public.agent_versions set status = 'archived' where tenant_id = %s",
                (two_tenants.b.id,),
            ).rowcount
            conn.commit()

        assert affected == 0

    def test_the_worker_of_a_cannot_insert_an_alert_into_b(
        self, dsn: str, two_tenants: TwoTenants
    ) -> None:
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            with pytest.raises(
                (psycopg.errors.InsufficientPrivilege, psycopg.errors.CheckViolation)
            ):
                create_alert(conn, two_tenants.b.id)


class TestTheLegitimatePathStillWorks:
    """A boundary that also blocks the owner is not security, it is an outage."""

    def test_the_worker_reads_its_own_active_version(
        self, dsn: str, two_tenants: TwoTenants, e2_rows: dict
    ) -> None:
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            rows = conn.execute(
                "select id from public.agent_versions where status = 'active'"
            ).fetchall()

        assert [row[0] for row in rows] == [e2_rows["a"].agent_version_id]

    def test_the_worker_reads_its_own_knowledge(
        self, dsn: str, two_tenants: TwoTenants, e2_rows: dict
    ) -> None:
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            rows = conn.execute("select id from public.knowledge_chunks").fetchall()

        assert len(rows) == 1

    def test_the_user_reads_the_versions_of_their_own_tenant(
        self, dsn: str, two_tenants: TwoTenants, e2_rows: dict
    ) -> None:
        with as_authenticated_user(dsn, two_tenants.a.user_id) as conn:
            rows = conn.execute("select id from public.agent_versions").fetchall()

        assert [row[0] for row in rows] == [e2_rows["a"].agent_version_id]


class TestTheEvaluationTablesAreOutOfTheDataApi:
    @pytest.mark.parametrize("table", INTERNAL_TABLES)
    @pytest.mark.parametrize("role", DATA_API_ROLES)
    def test_a_data_api_role_cannot_read_an_internal_table(
        self, dsn: str, role: str, table: str
    ) -> None:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(f"set role {role}")

                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(f"select * from {table}")


class TestTheRolesCannotOptOut:
    @pytest.mark.parametrize("table", E2_TABLES)
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

    @pytest.mark.parametrize("table", E2_TABLES)
    @pytest.mark.parametrize("role", ["worker_role", "sender_role"])
    def test_no_app_role_owns_an_e2_table(
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
    @pytest.mark.parametrize("table", E2_TABLES)
    def test_an_unset_tenant_scope_reads_nothing(
        self, dsn: str, e2_rows: dict, table: str
    ) -> None:
        """Fail closed. A pool that forgot to scope the unit of work sees zero rows."""
        with psycopg.connect(dsn) as conn:
            conn.execute("set role worker_role")
            try:
                rows = conn.execute(f"select id from {table}").fetchall()
            except psycopg.errors.InsufficientPrivilege:
                rows = []

        assert rows == []

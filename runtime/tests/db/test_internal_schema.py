"""The internal schema is internal — ADR-11, E1 · PR-0.

`public` is served over HTTP: it is in the exposed schemas of
`supabase/config.toml`. So the outbox and the raw webhook events do not live
there. They live in `internal`, which is not exposed and never will be, and the
Data API roles hold no privilege on it.

Two halves, like the pgmq suite of E0-08: the denial, and the access that has to
keep working. A suite that only proves denial would also pass with the tables
granted to nobody, which is the same as a platform that does not run.
"""

import psycopg
import pytest

from tests.db.conftest import TwoTenants, as_app_role
from tests.db.factories import Thread, create_outbox_item, create_thread, create_webhook_event

# `service_role` is on the list deliberately: internal tables get no grant here,
# not even for the role that bypasses everything else in the Data API.
DATA_API_ROLES = ("anon", "authenticated", "service_role")

INTERNAL_TABLES = ("internal.webhook_events", "internal.message_outbox")


@pytest.mark.parametrize("role", DATA_API_ROLES)
@pytest.mark.parametrize("table", INTERNAL_TABLES)
def test_the_data_api_roles_cannot_read_internal_tables(
    dsn: str, role: str, table: str
) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"set role {role}")

            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(f"select * from {table}")


@pytest.mark.parametrize("role", DATA_API_ROLES)
def test_the_data_api_roles_cannot_reach_the_schema_at_all(dsn: str, role: str) -> None:
    # Belt and braces over the table grants: without USAGE on the schema, a
    # future `grant select on internal.something to authenticated` written by
    # habit still resolves to nothing.
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"set role {role}")

            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("select 1 from internal.message_outbox limit 1")


def test_the_worker_can_record_a_webhook_event(
    dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
) -> None:
    with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
        event_id = create_webhook_event(conn, two_tenants.a.id)

    assert event_id > 0


def test_the_worker_can_queue_a_send_and_the_sender_can_perform_it(
    dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
) -> None:
    # The split that ADR-11 asks for, on one table: `agent_core` creates the
    # send inside the FASE 3 transaction, only `sender_role` performs it.
    thread: Thread = create_thread(admin, two_tenants.a.id)

    with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
        outbox_id = create_outbox_item(conn, two_tenants.a.id, thread)
        conn.commit()

    with as_app_role(dsn, "sender_role", two_tenants.a.id) as conn:
        affected = conn.execute(
            "update internal.message_outbox set status = 'sent' where id = %s",
            (outbox_id,),
        ).rowcount
        conn.commit()

    assert affected == 1


def test_the_sender_cannot_invent_a_send(
    dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
) -> None:
    # The other side of the same split. A sender that can INSERT is a sender
    # that can message a customer without the agent ever deciding to.
    thread: Thread = create_thread(admin, two_tenants.a.id)

    with as_app_role(dsn, "sender_role", two_tenants.a.id) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            create_outbox_item(conn, two_tenants.a.id, thread)

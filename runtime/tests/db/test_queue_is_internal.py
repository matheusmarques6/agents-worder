"""E0-08 · The queues are internal — ADR-11.

The pgmq schema is not in the exposed schemas of `supabase/config.toml`, which
is what keeps PostgREST from serving it. This suite asserts the second half of
that promise, the one a migration can undo by accident: the Data API roles hold
no privilege on the queue itself. A single `grant ... to authenticated` written
out of habit would hand every queued job — conversation ids, generations, whose
message is next — to any logged-in merchant, for every tenant at once.

`worker_role` is asserted from the other side on purpose: a suite that only
proves denial would also pass with the queue granted to nobody, which is the
same as a queue that does not work.

Note where the denial comes from: pgmq's functions carry EXECUTE through
PUBLIC and run as their caller, so a Data API role is stopped by the privilege
on the queue TABLE, not by the right to call `pgmq.send`. That is the boundary
worth testing, because it is the one a stray GRANT can open.
"""

import psycopg
import pytest

from agents_runtime.queueing import ALL_QUEUES, INBOUND

# The Data API roles. `service_role` is included deliberately: internal tables
# do not get a grant here, not even the one that bypasses everything else.
DATA_API_ROLES = ("anon", "authenticated", "service_role")

QUEUE_TABLE = f"pgmq.q_{INBOUND}"


@pytest.mark.parametrize("role", DATA_API_ROLES)
@pytest.mark.parametrize("queue", ALL_QUEUES)
def test_the_data_api_roles_cannot_read_the_queue(dsn: str, role: str, queue: str) -> None:
    # As oito: quatro de trabalho e quatro DLQs. Uma fila nova que escapasse
    # desta lista seria uma fila exposta, e o E0-08 deixou a cobranca escrita.
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"set role {role}")

            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(f"select * from pgmq.q_{queue}")


@pytest.mark.parametrize("role", DATA_API_ROLES)
def test_the_data_api_roles_cannot_enqueue(dsn: str, role: str) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"set role {role}")

            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("select pgmq.send(%s, %s)", (INBOUND, '{"forged": true}'))


@pytest.mark.parametrize("queue", ALL_QUEUES)
def test_the_worker_role_can_drive_the_queue(dsn: str, queue: str) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("set role worker_role")

            cur.execute("select pgmq.send(%s, %s)", (queue, '{"probe": true}'))
            message_id = cur.fetchone()[0]
            cur.execute("select pgmq.read(%s, 30, 1)", (queue,))
            cur.execute("select pgmq.archive(%s, %s::bigint)", (queue, message_id))
            archived = cur.fetchone()[0]

        conn.rollback()

    assert archived is True

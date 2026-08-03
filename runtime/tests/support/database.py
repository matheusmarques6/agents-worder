"""Where the test database lives.

One constant, imported by every level that talks to Postgres, so the `db`,
`rls` and `pipeline` suites can never drift into pointing at different
databases — which would show up as a passing suite that tested nothing.
"""

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg

# The port the Supabase CLI binds locally (supabase/config.toml [db].port).
DEFAULT_DSN = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"


def dsn_from_env() -> str:
    """The DSN of the database under test — CI overrides it, locally it is the CLI's."""
    return os.environ.get("SUPABASE_DB_URL", DEFAULT_DSN)


@asynccontextmanager
async def as_worker(dsn: str, tenant_id: uuid.UUID) -> AsyncIterator[psycopg.AsyncConnection]:
    """The runtime's own credential, scoped the way a unit of work scopes it.

    Async because the repository layer is: the runtime is asyncio, and a suite
    that exercised a synchronous copy would be proving code nobody runs.
    `is_local => true` mirrors the production discipline of `SET LOCAL
    app.tenant_id` per unit of work, and `SET ROLE` is what makes RLS apply —
    `worker_role` is not a superuser, has no BYPASSRLS and owns nothing.
    """
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await conn.execute("select set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        await conn.execute("set role worker_role")
        yield conn


@asynccontextmanager
async def as_runtime_worker(dsn: str) -> AsyncIterator[psycopg.AsyncConnection]:
    """A worker connection shaped exactly like the one `app.py` builds: autocommit,
    the role set, and **no tenant scope yet**.

    `as_worker` scopes the whole connection, which is right for testing a
    repository function. It is wrong for testing anything that has to open its
    OWN short transaction — a tool, the responder — because the scope would
    already be there and a tool that forgot to set it would still pass.
    """
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute("set role worker_role")
        yield conn


@asynccontextmanager
async def as_platform(dsn: str) -> AsyncIterator[psycopg.AsyncConnection]:
    """A privileged async connection — what the platform side of the product uses.

    Some writes do not belong to the runtime at all. Ingesting the merchant's
    knowledge is one: the worker processes hostile input from contacts, so it
    holds SELECT on `knowledge_chunks` and nothing more. Whoever ingests
    (the admin today, the onboarding of E4 tomorrow) is a different credential,
    and a test that arranges knowledge has to say so out loud.
    """
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        yield conn

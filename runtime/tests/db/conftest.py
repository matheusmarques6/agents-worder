"""Fixtures for the levels that talk to a real Postgres.

Deliberately NOT skippable. A security suite that quietly skips when the
database is missing is worse than no suite: the gate reports green and nobody
looks again. If Postgres is not reachable these tests fail, loudly.

Test data uses a per-run prefix (core/testes-e-cicd.md §3.3 item 15) so two
runs against the same database cannot collide, and teardown removes it.
"""

import asyncio
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
import pytest

from tests.support.database import dsn_from_env

if sys.platform == "win32":
    # Same reason as tests/pipeline/conftest.py (decisão 28): psycopg's async
    # connections need a selector loop and Windows defaults to proactor. Most
    # of this level is synchronous, but the repository layer is async — the
    # evaluation trail is written by the same code the runtime will run.
    #
    # The hook only EXISTS on win32: pytest-asyncio distinguishes "no
    # implementation" from "an implementation that declines", so a hook that
    # returned nothing elsewhere would not be the same thing as no hook.

    def pytest_asyncio_loop_factories(config: pytest.Config, item: pytest.Item) -> dict:
        return {"selector": asyncio.SelectorEventLoop}

# SET ROLE cannot take a bound parameter, so the role name is never allowed to
# come from data — only from this list.
APP_ROLES = ("worker_role", "sender_role")


@pytest.fixture(scope="session")
def dsn() -> str:
    return dsn_from_env()


@pytest.fixture
def admin(dsn: str) -> Iterator[psycopg.Connection]:
    """Superuser connection — used to arrange fixtures, never to assert access."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        yield conn


@dataclass(frozen=True)
class Tenant:
    id: uuid.UUID
    user_id: uuid.UUID
    membership_id: uuid.UUID


@dataclass(frozen=True)
class TwoTenants:
    """Tenant A and tenant B, each with its own user and membership.

    Two is the smallest number that can leak.
    """

    a: Tenant
    b: Tenant


def _create_tenant(conn: psycopg.Connection, label: str) -> Tenant:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()

    with conn.cursor() as cur:
        cur.execute(
            "insert into public.tenants (id, name) values (%s, %s)",
            (tenant_id, label),
        )
        cur.execute(
            """
            insert into auth.users (id, instance_id, aud, role, email, encrypted_password)
            values (%s, '00000000-0000-0000-0000-000000000000', 'authenticated',
                    'authenticated', %s, '')
            """,
            (user_id, f"{label}@example.test"),
        )
        cur.execute(
            "insert into public.profiles (user_id, full_name) values (%s, %s)",
            (user_id, label),
        )
        cur.execute(
            """
            insert into public.memberships (tenant_id, user_id, role)
            values (%s, %s, 'owner')
            returning id
            """,
            (tenant_id, user_id),
        )
        membership_id = cur.fetchone()[0]

    return Tenant(id=tenant_id, user_id=user_id, membership_id=membership_id)


@pytest.fixture
def two_tenants(admin: psycopg.Connection) -> Iterator[TwoTenants]:
    run = uuid.uuid4().hex[:8]
    tenants = TwoTenants(
        a=_create_tenant(admin, f"t-{run}-a"),
        b=_create_tenant(admin, f"t-{run}-b"),
    )

    yield tenants

    with admin.cursor() as cur:
        cur.execute(
            "delete from public.tenants where id = any(%s)",
            ([tenants.a.id, tenants.b.id],),
        )
        cur.execute(
            "delete from auth.users where id = any(%s)",
            ([tenants.a.user_id, tenants.b.user_id],),
        )


@contextmanager
def as_app_role(dsn: str, role: str, tenant_id: uuid.UUID) -> Iterator[psycopg.Connection]:
    """A connection acting as `worker_role`/`sender_role` scoped to one tenant.

    SET ROLE is enough to make RLS apply: the resulting role is not a superuser,
    has no BYPASSRLS and does not own the tables — the same three properties the
    production pools have, asserted separately in the leak suite.

    The scope is transaction-local (`is_local => true`) on purpose: the
    production discipline is `SET LOCAL app.tenant_id` per unit of work, and
    the harness must exercise the same lifetime the driver will use.
    """
    if role not in APP_ROLES:
        raise ValueError(f"unknown app role: {role}")

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("select set_config('app.tenant_id', %s, true)", (str(tenant_id),))
            cur.execute(f"set role {role}")
        yield conn


@contextmanager
def as_authenticated_user(dsn: str, user_id: uuid.UUID) -> Iterator[psycopg.Connection]:
    """A connection acting as the hub does: the `authenticated` role plus JWT claims.

    `auth.uid()` reads `request.jwt.claims`, so setting it reproduces exactly
    what PostgREST does with a verified token — without needing to sign one.
    PostgREST sets the claims transaction-locally, so the harness does too.
    """
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select set_config('request.jwt.claims', %s, true)",
                (f'{{"sub": "{user_id}", "role": "authenticated"}}',),
            )
            cur.execute("set role authenticated")
        yield conn

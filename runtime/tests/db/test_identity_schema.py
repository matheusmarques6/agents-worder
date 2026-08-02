"""Schema behaviour of the identity tables that RLS does not cover.

The leak suite proves who can see a row; this file proves the row itself
behaves as the data dictionary describes it.
"""

import uuid

import psycopg


class TestUpdatedAtIsMaintained:
    def test_updating_a_tenant_touches_updated_at(self, admin: psycopg.Connection) -> None:
        """`updated_at` that never updates is worse than absent: it reads as
        "this row never changed" with authority. The column moves on UPDATE,
        by trigger, so no write path can forget it."""
        tenant_id = uuid.uuid4()
        admin.execute(
            "insert into public.tenants (id, name) values (%s, 'touch-test')",
            (tenant_id,),
        )
        try:
            before = admin.execute(
                "select updated_at from public.tenants where id = %s", (tenant_id,)
            ).fetchone()[0]

            admin.execute(
                "update public.tenants set name = 'touched' where id = %s", (tenant_id,)
            )
            after = admin.execute(
                "select updated_at from public.tenants where id = %s", (tenant_id,)
            ).fetchone()[0]
        finally:
            admin.execute("delete from public.tenants where id = %s", (tenant_id,))

        assert after > before

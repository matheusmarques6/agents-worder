"""S4 — `claim_due_touches`, a varredura dos toques vencidos.

The mould is `claim_outbox_batch`, and so is the justification: sweeping every
tenant's schedule is cross-tenant by nature, no application role may hold a
global SELECT (ADR-11), and therefore the sweep is a SECURITY DEFINER claim
function with a fixed `search_path`, EXECUTE revoked from PUBLIC — and **no
filter parameter**. A dispatcher that could ask for "only tenant X" would be an
arbitrary cross-tenant query wearing a claim function's name.

Claiming is a WRITE, not a read: the row is marked `enqueued` inside the same
statement that selects it, which is what stops the next minute's pass from
handing the same touch to a second job. `FOR UPDATE SKIP LOCKED` is what lets a
second pass — or a second process, the day there is one — take a disjoint batch
instead of queueing behind the first.
"""

import uuid

import psycopg
import pytest

from tests.db.conftest import TwoTenants, as_app_role
from tests.db.factories import create_contact
from tests.db.factories_e3 import create_funnel, create_scheduled_touch

pytestmark = pytest.mark.db

DUE = -60
LATER = 3600


def claim(conn: psycopg.Connection, *, limit: int = 100) -> list[tuple]:
    return conn.execute(
        "select * from internal.claim_due_touches(%s, %s)", (uuid.uuid4(), limit)
    ).fetchall()


def touch_row(conn: psycopg.Connection, touch_id: uuid.UUID) -> tuple:
    return conn.execute(
        "select status, claimed_by is not null, claimed_at is not null"
        "  from public.scheduled_touches where id = %s",
        (touch_id,),
    ).fetchone()


def a_touch(
    conn: psycopg.Connection, tenant_id, *, due_in_seconds: int = DUE, **kwargs
) -> uuid.UUID:
    funnel = create_funnel(conn, tenant_id, occasion=kwargs.pop("occasion", "cart_abandoned"))
    contact = create_contact(conn, tenant_id)
    return create_scheduled_touch(
        conn, tenant_id, funnel.id, contact, due_in_seconds=due_in_seconds, **kwargs
    )


class TestWhatIsSwept:
    def test_a_due_pending_touch_is_claimed(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        touch_id = a_touch(admin, two_tenants.a.id)

        claimed = claim(admin)

        assert claimed == [(touch_id, two_tenants.a.id)]

    def test_the_claim_marks_the_row_so_the_next_pass_walks_past_it(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # Without the mark, the minute tick would hand the same touch to a
        # second job every minute until it fired — and the outbox's UNIQUE would
        # be the only thing between a merchant and a duplicate send.
        touch_id = a_touch(admin, two_tenants.a.id)

        claim(admin)

        assert touch_row(admin, touch_id) == ("enqueued", True, True)
        assert claim(admin) == []

    def test_a_touch_that_is_not_due_yet_is_left_alone(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        a_touch(admin, two_tenants.a.id, due_in_seconds=LATER)

        assert claim(admin) == []

    @pytest.mark.parametrize(
        ("status", "extra"),
        [
            ("enqueued", {}),
            ("sent", {"sent_ago_seconds": 30}),
            ("cancelled", {"cancel_reason": "suppressed_block"}),
        ],
    )
    def test_only_a_pending_touch_is_claimable(
        self, admin: psycopg.Connection, two_tenants: TwoTenants, status: str, extra: dict
    ) -> None:
        a_touch(admin, two_tenants.a.id, status=status, **extra)

        assert claim(admin) == []

    def test_the_sweep_crosses_tenants(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # The whole reason the function is SECURITY DEFINER. One process serves
        # every tenant, and a sweep scoped to one would need a caller that
        # already knew which — which is the question the sweep exists to answer.
        a_touch(admin, two_tenants.a.id)
        a_touch(admin, two_tenants.b.id)

        assert {row[1] for row in claim(admin)} == {two_tenants.a.id, two_tenants.b.id}

    def test_the_batch_is_bounded(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        for occasion in ("cart_abandoned", "checkout_abandoned", "pix_pending"):
            a_touch(admin, two_tenants.a.id, occasion=occasion)

        assert len(claim(admin, limit=2)) == 2

    def test_only_what_was_assigned_comes_back(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # `SKIP LOCKED`, asserted the only way it can be: a row somebody else is
        # holding is not in the batch, and the claim does not wait for it.
        mine = a_touch(admin, two_tenants.a.id, occasion="cart_abandoned")
        theirs = a_touch(admin, two_tenants.a.id, occasion="pix_pending")

        with psycopg.connect(dsn) as holder:
            holder.execute(
                "select id from public.scheduled_touches where id = %s for update", (theirs,)
            )

            claimed = claim(admin)

        assert claimed == [(mine, two_tenants.a.id)]
        # And the one that was held is still pending — skipped, not consumed.
        assert touch_row(admin, theirs)[0] == "pending"


class TestWhoMayRun:
    def test_the_worker_sweeps(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        a_touch(admin, two_tenants.a.id)

        with as_app_role(dsn, "worker_role", uuid.uuid4()) as conn:
            assert len(claim(conn)) == 1
            conn.commit()

    def test_the_sender_does_not_sweep(self, dsn: str) -> None:
        # The asymmetry of the outbox, in the other direction: the sender drains
        # what this produces and has no business producing one.
        with as_app_role(dsn, "sender_role", uuid.uuid4()) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                claim(conn)

    def test_the_data_api_does_not_sweep(self, dsn: str) -> None:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("set role authenticated")

                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("select * from internal.claim_due_touches(gen_random_uuid())")

    def test_the_three_definer_guards_are_in_place(self, admin: psycopg.Connection) -> None:
        prosecdef, proconfig = admin.execute(
            "select prosecdef, proconfig from pg_proc where proname = 'claim_due_touches'"
        ).fetchone()

        assert prosecdef is True
        assert any(setting.startswith("search_path=") for setting in proconfig)

    def test_the_signature_offers_no_way_to_ask_for_one_tenant(
        self, admin: psycopg.Connection
    ) -> None:
        # ADR-11 in one assertion: the defence is the signature. A filter
        # parameter — any filter parameter — would make this an arbitrary
        # cross-tenant query with a claim function's name.
        arguments = admin.execute(
            """
            select pg_get_function_arguments(oid)
              from pg_proc where proname = 'claim_due_touches'
            """
        ).fetchone()[0]

        assert "tenant" not in arguments
        assert arguments == "p_worker_id uuid, p_limit integer DEFAULT 100"

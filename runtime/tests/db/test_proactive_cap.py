"""E3 · S2 · D1 — who may loosen the platform ceiling, and how.

RF-034 gives every contact one proactive touch per 24h by default and lets the
admin raise a tenant up to four. The merchant may only tighten. That asymmetry
is a **safety lock**, exactly like Judge 1 being platform-fixed: a gate a
customer can reconfigure is not a gate.

So the column has one write path — `internal.set_proactive_cap` — and three
things have to be true at once for that sentence to mean anything:

1. the value is bounded by a CHECK, so no path at all can exceed four;
2. the merchant's credential cannot UPDATE the column (privilege), which today
   is the whole of the hub's reach into `tenants`;
3. even a credential that *does* hold UPDATE cannot move this column, because a
   trigger refuses any change that did not come through the function. Point 2
   alone would be a green that a single blanket `GRANT UPDATE ON tenants` in E5
   would silently turn red — and nobody would notice, because the test would
   still pass for the wrong reason (decisão 16: "permission denied" is not
   evidence of a rule).

The change is audited, because RNF-044 wants consent and its opposite on the
record and because "who raised this tenant to four" is the first question after
a merchant is accused of spamming.
"""

import uuid

import psycopg
import pytest

from tests.db.conftest import TwoTenants, as_app_role, as_authenticated_user


def cap_of(conn: psycopg.Connection, tenant_id: uuid.UUID) -> int:
    return conn.execute(
        "select proactive_max_per_contact_24h from public.tenants where id = %s",
        (tenant_id,),
    ).fetchone()[0]


class TestTheFunctionIsTheWritePath:
    def test_the_admin_raises_a_tenant_towards_the_ceiling(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        admin.execute(
            "select internal.set_proactive_cap(%s, %s, %s)",
            (two_tenants.a.id, 4, two_tenants.a.user_id),
        )

        assert cap_of(admin, two_tenants.a.id) == 4

    def test_tightening_goes_through_the_same_door(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        admin.execute(
            "select internal.set_proactive_cap(%s, %s, %s)",
            (two_tenants.a.id, 3, two_tenants.a.user_id),
        )
        admin.execute(
            "select internal.set_proactive_cap(%s, %s, %s)",
            (two_tenants.a.id, 1, two_tenants.a.user_id),
        )

        assert cap_of(admin, two_tenants.a.id) == 1

    def test_the_ceiling_is_not_restated_by_the_function(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # The number 4 exists in exactly one place: the CHECK. A second copy
        # inside the function is a copy that drifts, so the function lets the
        # constraint speak.
        with pytest.raises(psycopg.errors.CheckViolation):
            admin.execute(
                "select internal.set_proactive_cap(%s, %s, %s)",
                (two_tenants.a.id, 5, two_tenants.a.user_id),
            )

    def test_an_unknown_tenant_is_an_error_not_a_silent_no_op(
        self, admin: psycopg.Connection
    ) -> None:
        with pytest.raises(psycopg.errors.RaiseException):
            admin.execute(
                "select internal.set_proactive_cap(%s, %s, null)", (uuid.uuid4(), 2)
            )

    def test_the_change_lands_in_the_audit_log(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        admin.execute(
            "select internal.set_proactive_cap(%s, %s, %s)",
            (two_tenants.a.id, 4, two_tenants.a.user_id),
        )

        row = admin.execute(
            """
            select actor_type, actor_user_id, action, payload
              from public.audit_log
             where tenant_id = %s and action = 'tenant.proactive_cap_set'
            """,
            (two_tenants.a.id,),
        ).fetchone()

        assert row is not None
        assert row[0] == "user"
        assert row[1] == two_tenants.a.user_id
        assert row[3]["from"] == 1
        assert row[3]["to"] == 4

    def test_a_rejected_change_leaves_no_audit_trail(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # One transaction: the constraint rolls the audit row back with the
        # update. An audit log that records attempts as facts is worse than none.
        with psycopg.connect(dsn) as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "select internal.set_proactive_cap(%s, %s, %s)",
                    (two_tenants.a.id, 9, two_tenants.a.user_id),
                )
            conn.rollback()

        entries = admin.execute(
            "select count(*) from public.audit_log where tenant_id = %s",
            (two_tenants.a.id,),
        ).fetchone()[0]

        assert entries == 0
        assert cap_of(admin, two_tenants.a.id) == 1


class TestTheMerchantPathCannotLoosenIt:
    def test_the_hub_credential_has_no_update_on_the_column(
        self, dsn: str, two_tenants: TwoTenants
    ) -> None:
        # The merchant's own tenant, the merchant's own JWT: still no.
        with as_authenticated_user(dsn, two_tenants.a.user_id) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    """
                    update public.tenants set proactive_max_per_contact_24h = 4
                     where id = %s
                    """,
                    (two_tenants.a.id,),
                )

    def test_the_column_privilege_is_absent_by_name(self, admin: psycopg.Connection) -> None:
        # Stated against the catalogue, so that a future `GRANT UPDATE ON
        # tenants TO authenticated` in E5 fails this test instead of quietly
        # handing the ceiling to every merchant.
        granted = admin.execute(
            """
            select has_column_privilege('authenticated', 'public.tenants',
                                        'proactive_max_per_contact_24h', 'UPDATE')
            """
        ).fetchone()[0]

        assert granted is False

    def test_the_runtime_cannot_move_it_either(
        self, dsn: str, two_tenants: TwoTenants
    ) -> None:
        # The worker processes hostile input from contacts. Whatever a prompt
        # injection convinces the model of, the ceiling is not reachable.
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    """
                    update public.tenants set proactive_max_per_contact_24h = 4
                     where id = %s
                    """,
                    (two_tenants.a.id,),
                )

    def test_even_a_credential_with_update_is_refused_by_the_guard(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # The superuser: every privilege there is, and still no. This is the
        # half of the lock that does not depend on a GRANT staying absent.
        with pytest.raises(psycopg.errors.RaiseException, match="set_proactive_cap"):
            admin.execute(
                "update public.tenants set proactive_max_per_contact_24h = 4 where id = %s",
                (two_tenants.a.id,),
            )

    def test_an_unrelated_update_to_the_tenant_still_works(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # A guard that blocks the whole table is an outage, not a lock.
        admin.execute(
            "update public.tenants set never_say_ai = false where id = %s",
            (two_tenants.a.id,),
        )

        value = admin.execute(
            "select never_say_ai from public.tenants where id = %s", (two_tenants.a.id,)
        ).fetchone()[0]

        assert value is False


class TestNobodyInTheApplicationMayCallTheFunction:
    """ADR-11: EXECUTE revoked from PUBLIC, and granted to nobody by default.

    The admin plane connects with its own credential (E6). The runtime pools and
    the Data API roles have no business raising a rate limit.
    """

    @pytest.mark.parametrize(
        "role", ["worker_role", "sender_role", "anon", "authenticated", "service_role"]
    )
    def test_the_role_cannot_execute_it(
        self, admin: psycopg.Connection, role: str
    ) -> None:
        granted = admin.execute(
            """
            select has_function_privilege(
                %s, 'internal.set_proactive_cap(uuid, integer, uuid)', 'EXECUTE')
            """,
            (role,),
        ).fetchone()[0]

        assert granted is False

    def test_public_cannot_execute_it(self, admin: psycopg.Connection) -> None:
        # A function with a NULL ACL is a function PUBLIC may execute — the
        # default nobody writes down. The revoke has to be visible here.
        acl = admin.execute(
            """
            select proacl from pg_proc
             where oid = 'internal.set_proactive_cap(uuid, integer, uuid)'::regprocedure
            """
        ).fetchone()[0]

        assert acl is not None
        assert not any(str(entry).startswith("=") for entry in acl)

    def test_the_search_path_is_fixed(self, admin: psycopg.Connection) -> None:
        settings = admin.execute(
            """
            select proconfig from pg_proc
             where oid = 'internal.set_proactive_cap(uuid, integer, uuid)'::regprocedure
            """
        ).fetchone()[0]

        assert any(entry.startswith("search_path=") for entry in settings or [])

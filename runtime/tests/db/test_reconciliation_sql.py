"""The three SQL guarantees the poll stands on.

The reconciliation of S8 has no write path of its own (D5), so what the
database owes it is small and exact:

  * a cross-tenant claim of the stores nobody has asked in a while;
  * a closing that writes `sync_status` and `last_sync_at` — the two columns
    that existed since E1 with no writer at all, which is the milestone's own
    disease: a column nobody writes lies exactly like a guard with no target;
  * a cursor that only ever moves forward.

The third one is the load-bearing one, and it is a property of the COLUMN
rather than a discipline of the callers. "Never regresses" spread across three
call sites is a rule that holds until the fourth.
"""

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from tests.db.conftest import TwoTenants
from tests.db.factories import create_connector_account

pytestmark = pytest.mark.db

FIFTEEN_MINUTES = timedelta(minutes=15)


def _claim(conn: psycopg.Connection, *, stale_after=FIFTEEN_MINUTES, limit: int = 20) -> list:
    with conn.cursor() as cur:
        cur.execute(
            "select * from internal.claim_sync_targets(%s, %s)", (stale_after, limit)
        )
        return cur.fetchall()


def _finish(conn: psycopg.Connection, account_id, status: str, cursor_at=None):
    with conn.cursor() as cur:
        cur.execute(
            "select internal.finish_sync(%s, %s, %s)", (account_id, status, cursor_at)
        )
        return cur.fetchone()[0]


def _account_row(conn: psycopg.Connection, account_id) -> tuple:
    with conn.cursor() as cur:
        cur.execute(
            "select sync_status, last_sync_at, sync_cursor_at"
            "  from public.connector_accounts where id = %s",
            (account_id,),
        )
        return cur.fetchone()


class TestTheCursorNeverRegresses:
    """The one rule that cannot be a convention."""

    def test_an_older_cursor_is_refused_and_the_stored_one_is_returned(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        account = create_connector_account(admin, two_tenants.a.id)
        ahead = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
        behind = datetime(2026, 8, 6, 11, 0, tzinfo=UTC)

        _finish(admin, account.id, "ok", ahead)
        # A pass that read a stale page — a retried job, a slow replica, an
        # adapter that paginated backwards. Accepting this would re-deliver an
        # hour of events on every tick forever, and the D5 dedup would hide it
        # perfectly: no duplicate effect, just a poll that never finishes.
        returned = _finish(admin, account.id, "ok", behind)

        assert returned == ahead
        assert _account_row(admin, account.id)[2] == ahead

    def test_a_pass_that_saw_nothing_keeps_the_cursor(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        account = create_connector_account(admin, two_tenants.a.id)
        at = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
        _finish(admin, account.id, "ok", at)

        # No events since last time. NULL means "nothing to say about the
        # cursor", never "rewind to the beginning of time".
        assert _finish(admin, account.id, "ok", None) == at

    def test_the_first_sync_adopts_the_cursor_it_is_given(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        account = create_connector_account(admin, two_tenants.a.id)
        assert _account_row(admin, account.id)[2] is None

        at = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
        assert _finish(admin, account.id, "ok", at) == at

    def test_a_failed_pass_keeps_what_it_managed_to_ingest(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        account = create_connector_account(admin, two_tenants.a.id)
        ingested_up_to = datetime(2026, 8, 6, 11, 30, tzinfo=UTC)

        # Died after event two of five. The cursor reaches the last event the
        # ingestion ACCEPTED, never the end of the window that was asked for —
        # that is the whole difference between "resume" and "skip".
        assert _finish(admin, account.id, "error", ingested_up_to) == ingested_up_to
        assert _account_row(admin, account.id)[0] == "error"


class TestTheColumnsFinallyHaveAWriter:
    def test_closing_writes_status_and_the_instant(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        account = create_connector_account(admin, two_tenants.a.id)
        assert _account_row(admin, account.id)[1] is None

        _finish(admin, account.id, "ok", None)
        status, last_sync_at, _ = _account_row(admin, account.id)

        assert status == "ok"
        assert last_sync_at is not None

    def test_syncing_is_not_a_way_to_finish(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # `syncing` belongs to the claim. Closing into it would be closing
        # without closing, and the account would look busy forever.
        account = create_connector_account(admin, two_tenants.a.id)
        with pytest.raises(psycopg.errors.RaiseException, match="inválido"):
            _finish(admin, account.id, "syncing", None)

    def test_finishing_a_store_that_does_not_exist_raises(
        self, admin: psycopg.Connection
    ) -> None:
        # Silent no-op would mean a pass reporting success against nothing.
        with pytest.raises(psycopg.errors.RaiseException, match="inexistente"):
            _finish(admin, uuid.uuid4(), "ok", None)


class TestTheClaim:
    def test_a_never_synced_store_is_due(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        account = create_connector_account(admin, two_tenants.a.id)

        claimed = {row[0] for row in _claim(admin)}

        assert account.id in claimed
        assert _account_row(admin, account.id)[0] == "syncing"

    def test_a_store_just_asked_is_not_due_again(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        account = create_connector_account(admin, two_tenants.a.id)
        _finish(admin, account.id, "ok", None)

        assert account.id not in {row[0] for row in _claim(admin)}

    def test_a_pass_that_died_in_syncing_is_asked_again(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        account = create_connector_account(admin, two_tenants.a.id)
        _claim(admin)  # leaves it `syncing`, as a crashed pass would

        # Freshness, not status, decides — otherwise a process killed mid-pass
        # would take its stores out of reconciliation permanently, which is the
        # one outcome the safety belt exists to prevent.
        assert account.id in {row[0] for row in _claim(admin, stale_after=timedelta(0))}

    def test_the_claim_carries_the_cursor_and_the_store_key(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        account = create_connector_account(admin, two_tenants.a.id)
        at = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
        _finish(admin, account.id, "ok", at)

        (row,) = [r for r in _claim(admin, stale_after=timedelta(0)) if r[0] == account.id]
        _id, tenant_id, platform, source_account_id, cursor_at = row

        # Everything `ingest_webhook` will be called with, in one read: the poll
        # never queries the store again to find out who it belongs to.
        assert tenant_id == two_tenants.a.id
        assert platform == "shopify"
        assert source_account_id == account.source_account_id
        assert cursor_at == at

    def test_the_ingestion_role_may_claim_and_finish(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # The pass runs as `ingestion_role`, because it calls `ingest_webhook`
        # and that is whose key it needs. The claim crosses tenants and the
        # close writes a table this role has no privilege on at all — both work
        # only because both are SECURITY DEFINER, and a grant that quietly went
        # missing would surface here and nowhere else.
        account = create_connector_account(admin, two_tenants.a.id)

        with psycopg.connect(dsn) as conn:
            conn.execute("set role ingestion_role")
            claimed = {row[0] for row in _claim(conn)}
            assert account.id in claimed
            _finish(conn, account.id, "ok", datetime(2026, 8, 6, 12, 0, tzinfo=UTC))
            conn.commit()

        assert _account_row(admin, account.id)[0] == "ok"

    def test_the_worker_may_not_reconcile(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # The other half, and the one that matters. `test_the_worker_cannot_
        # ingest_either` (E1) says the worker is not the ingestion; a sweep
        # wired onto the worker's pool would have made that sentence false
        # without editing it. Both doors of the pass are shut to it.
        create_connector_account(admin, two_tenants.a.id)

        with psycopg.connect(dsn) as conn:
            conn.execute("set role worker_role")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                _claim(conn)
        with psycopg.connect(dsn) as conn:
            conn.execute("set role worker_role")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                _finish(conn, uuid.uuid4(), "ok", None)

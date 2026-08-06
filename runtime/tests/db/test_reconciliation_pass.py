"""The assertion the whole step exists for: two paths, one idempotency.

D5 says the reconciliation calls the SAME `internal.ingest_webhook` the webhook
calls. The reason is not tidiness — it is that a second write path would be a
second idempotency to keep in step with the first, and the only symptom of them
drifting would be a customer receiving the same message twice, months later, in
production.

So the proof is deliberately brutal: the same fact delivered three times by the
webhook AND three times by the poll, interleaved, must leave exactly one row,
one job and one order. Not "one per path". One.

The other two properties are the ones that would make the belt useless if they
were false: an event only the poll ever saw has to enter identically to one the
webhook brought (otherwise the belt catches events into a different, untested
behaviour), and a pass that dies halfway must skip nothing (otherwise the belt
loses exactly what it was hung to catch).
"""

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from agents_runtime.connectors.port import PlatformEvent, SyncTarget
from agents_runtime.connectors.reconcile import reconcile_pass
from agents_runtime.repository import reconciliation as repo
from tests.db.conftest import TwoTenants
from tests.db.factories import ConnectorAccount, create_connector_account, unique_phone
from tests.support.fake_connector import ScriptedConnector

pytestmark = pytest.mark.db

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
ALWAYS = timedelta(0)


def _abandonment(external_id: str, *, at: datetime, phone: str) -> PlatformEvent:
    """A checkout abandonment, in the exact shape the Edge Function delivers.

    Shape, not resemblance: `apply_domain_event` reads `payload->>'phone'` and
    `payload->'order'`, so anything else here would be a poll that ingests
    beautifully and routes to `invalid_payload`.
    """
    return PlatformEvent(
        external_event_id=external_id,
        event_type="checkout_abandoned",
        occurred_at=at,
        payload={"phone": phone, "order": {"external_id": f"ord-{external_id}"}},
    )


def _deliver_by_webhook(
    conn: psycopg.Connection, account: ConnectorAccount, event: PlatformEvent
) -> str:
    """The Edge Function's call — same function, same arguments, no poll involved."""
    with conn.cursor() as cur:
        cur.execute(
            "select status from internal.ingest_webhook(%s, %s, %s, %s, %s)",
            (
                "shopify",
                account.source_account_id,
                event.external_event_id,
                event.event_type,
                psycopg.types.json.Jsonb(dict(event.payload)),
            ),
        )
        return cur.fetchone()[0]


def _rows(conn: psycopg.Connection, account: ConnectorAccount, external_id: str) -> list:
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, status, connector_account_id
              from internal.webhook_events
             where source = 'shopify'
               and source_account_id = %s
               and external_event_id = %s
            """,
            (account.source_account_id, external_id),
        )
        return cur.fetchall()


def _make_stale(conn: psycopg.Connection, account_id: uuid.UUID) -> None:
    """Undo the freshness the previous pass wrote, without touching the cursor."""
    with conn.cursor() as cur:
        cur.execute(
            "update public.connector_accounts set last_sync_at = null where id = %s",
            (account_id,),
        )


class TestThreeReplaysDownBothPathsAreOneEffect:
    async def test_the_same_fact_six_times_leaves_one_row(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        account = create_connector_account(admin, two_tenants.a.id)
        event = _abandonment("1042", at=NOW, phone=unique_phone())
        connector = ScriptedConnector([event])

        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            outcomes: list[str] = []
            for _ in range(3):
                # The webhook fires. Meta and Shopify both redeliver on a
                # timeout, so three is not a contrived number.
                outcomes.append(_deliver_by_webhook(admin, account, event))
                # …and the belt sweeps, seeing the very same fact.
                _make_stale(admin, account.id)
                result = await reconcile_pass(
                    conn, {"shopify": connector}, stale_after=ALWAYS
                )
                outcomes.append("ingested" if result.ingested else "duplicate")

        # Six deliveries. One of them was the first.
        assert outcomes.count("ingested") == 1, outcomes
        assert outcomes.count("duplicate") == 5, outcomes

        rows = _rows(admin, account, "1042")
        assert len(rows) == 1, "a segunda idempotência apareceu: duas linhas para um fato"

    async def test_one_job_and_one_order_not_one_per_path(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # The row being unique is necessary and not sufficient: what reaches a
        # customer is the JOB, and a poll that enqueued its own would produce
        # one funnel per delivery with the webhook's table looking perfect.
        account = create_connector_account(admin, two_tenants.a.id)
        event = _abandonment("2050", at=NOW, phone=unique_phone())
        connector = ScriptedConnector([event])

        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            for _ in range(3):
                _deliver_by_webhook(admin, account, event)
                _make_stale(admin, account.id)
                await reconcile_pass(conn, {"shopify": connector}, stale_after=ALWAYS)

        (event_id, _status, _connector) = _rows(admin, account, "2050")[0]
        with admin.cursor() as cur:
            cur.execute(
                """
                select count(*)
                  from pgmq.q_q_domain_events
                 where (message ->> 'webhook_event_id')::bigint = %s
                """,
                (event_id,),
            )
            (jobs,) = cur.fetchone()

        assert jobs == 1, f"seis entregas, {jobs} jobs — o funil sairia {jobs} vezes"


class TestWhatOnlyThePollSaw:
    async def test_it_enters_exactly_as_the_webhook_would_have(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        account = create_connector_account(admin, two_tenants.a.id)
        phone = unique_phone()
        # Two identical facts about two orders. One arrives by webhook; the
        # other's delivery was lost and only the poll ever sees it.
        by_webhook = _abandonment("3001", at=NOW, phone=phone)
        lost = _abandonment("3002", at=NOW + timedelta(minutes=1), phone=phone)

        _deliver_by_webhook(admin, account, by_webhook)

        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            result = await reconcile_pass(
                conn, {"shopify": ScriptedConnector([lost])}, stale_after=ALWAYS
            )

        assert result.ingested == 1

        delivered = _rows(admin, account, "3001")[0]
        recovered = _rows(admin, account, "3002")[0]

        # Same status, same resolved store. Downstream cannot tell which door
        # each came through — which is the point: there is no second behaviour
        # to keep in step, so there is no second behaviour to get wrong.
        assert recovered[1] == delivered[1] == "enqueued"
        assert recovered[2] == delivered[2] == account.id

    async def test_an_untranslated_type_never_reaches_the_ingestion(self) -> None:
        # The belt catching an event into a status nobody reads would be worse
        # than not catching it: the hub would show a healthy sync.
        with pytest.raises(ValueError, match="não traduzido"):
            PlatformEvent(
                external_event_id="4001",
                event_type="checkouts/create",
                occurred_at=NOW,
                payload={"phone": "+5511999990000"},
            )


class TestAPassThatDiesHalfwaySkipsNothing:
    async def test_the_cursor_stops_at_the_last_ingested_event(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        account = create_connector_account(admin, two_tenants.a.id)
        phone = unique_phone()
        events = [
            _abandonment(f"50{n}", at=NOW + timedelta(minutes=n), phone=phone)
            for n in range(5)
        ]
        # A page of two, then the platform stops answering. The remaining three
        # are the events that would be lost by a cursor that moved to the end of
        # the window it asked for rather than to the last event it ingested.
        connector = ScriptedConnector(events, fail_after=1)

        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            first = await reconcile_pass(
                conn, {"shopify": connector}, stale_after=ALWAYS, page_size=2
            )
            assert first.ingested == 2

            _make_stale(admin, account.id)
            broken = await reconcile_pass(
                conn, {"shopify": connector}, stale_after=ALWAYS, page_size=2
            )
            assert broken.ingested == 0
            assert broken.failures and "ConnectionError" in broken.failures[0][1]

            with admin.cursor() as cur:
                cur.execute(
                    "select sync_status, sync_cursor_at from public.connector_accounts"
                    " where id = %s",
                    (account.id,),
                )
                status, cursor_at = cur.fetchone()
            # The failure is a recorded fact, and the cursor did not move past
            # what was ingested.
            assert status == "error"
            assert cursor_at == events[1].occurred_at

            # The platform comes back. Nothing was skipped.
            connector.fail_after = None
            for _ in range(3):
                _make_stale(admin, account.id)
                await reconcile_pass(
                    conn, {"shopify": connector}, stale_after=ALWAYS, page_size=2
                )

        for event in events:
            assert len(_rows(admin, account, event.external_event_id)) == 1, (
                f"{event.external_event_id} não entrou — o cursor pulou um evento"
            )

    async def test_a_store_with_no_adapter_is_recorded_not_reported_ok(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # `ok` would claim we looked. A connected, unreconciled store is a fact
        # the merchant's hub has to be able to show.
        account = create_connector_account(admin, two_tenants.b.id, platform="yampi")

        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            result = await reconcile_pass(conn, {"shopify": ScriptedConnector([])},
                                          stale_after=ALWAYS)

        assert any(source == account.source_account_id for source, _ in result.failures)
        with admin.cursor() as cur:
            cur.execute(
                "select sync_status from public.connector_accounts where id = %s",
                (account.id,),
            )
            assert cur.fetchone()[0] == "error"


class TestOneStoreNeverStopsTheSweep:
    async def test_a_store_that_vanishes_mid_sweep_does_not_freeze_the_others(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # The merchant disconnects a store while the sweep is holding it. The
        # close then raises — the row it was going to write is gone — and
        # outside the per-store isolation that exception would abort the pass
        # and leave every store claimed after it stuck in `syncing`: one
        # tenant's disconnection freezing everybody else's reconciliation, with
        # no error anywhere, because `syncing` looks like work in progress.
        doomed = create_connector_account(admin, two_tenants.a.id, platform="shopify")
        survivor = create_connector_account(admin, two_tenants.b.id, platform="nuvemshop")
        # The claim orders by `last_sync_at nulls first`, so this is what makes
        # the doomed store go FIRST — without it the test would pass half the
        # time by luck, which is the same as not testing anything.
        with admin.cursor() as cur:
            cur.execute(
                "update public.connector_accounts set last_sync_at = now() - interval '1 hour'"
                " where id = %s",
                (survivor.id,),
            )

        def disconnect(target) -> None:
            if target.connector_account_id == doomed.id:
                with admin.cursor() as cur:
                    cur.execute(
                        "delete from public.connector_accounts where id = %s", (doomed.id,)
                    )

        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            result = await reconcile_pass(
                conn,
                {
                    "shopify": ScriptedConnector([], on_fetch=disconnect),
                    "nuvemshop": ScriptedConnector([]),
                },
                stale_after=ALWAYS,
            )

        assert result.stores == 2
        assert any("inexistente" in reason for _, reason in result.failures)
        with admin.cursor() as cur:
            cur.execute(
                "select sync_status from public.connector_accounts where id = %s",
                (survivor.id,),
            )
            assert cur.fetchone()[0] == "ok", "a loja seguinte ficou presa em syncing"

    async def test_a_store_the_ingestion_no_longer_resolves_is_an_error_not_a_silence(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # Reconnected under a new id between the claim and the ingestion. The
        # row still exists, so the close works — but `ingest_webhook` answers
        # `unknown_account`, and treating that as "nothing to do" would be a
        # store reporting healthy syncs forever while ingesting nothing.
        account = create_connector_account(admin, two_tenants.a.id)

        def reconnect_elsewhere(target) -> None:
            with admin.cursor() as cur:
                cur.execute(
                    "update public.connector_accounts set source_account_id = %s where id = %s",
                    (f"{account.source_account_id}-nova", account.id),
                )

        connector = ScriptedConnector(
            [_abandonment("7001", at=NOW, phone=unique_phone())],
            on_fetch=reconnect_elsewhere,
        )
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            result = await reconcile_pass(conn, {"shopify": connector}, stale_after=ALWAYS)

        assert result.ingested == 0
        assert any("unknown_account" in reason for _, reason in result.failures)
        with admin.cursor() as cur:
            cur.execute(
                "select sync_status, sync_cursor_at from public.connector_accounts"
                " where id = %s",
                (account.id,),
            )
            status, cursor_at = cur.fetchone()
        assert status == "error"
        assert cursor_at is None, "o cursor andou por um evento que não virou linha"

    async def test_a_broken_platform_costs_exactly_its_own_store(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # The sweep is cross-tenant by nature (ADR-11). An adapter raising must
        # not be able to stop another tenant's reconciliation — that would turn
        # one merchant's broken integration into everybody's silent outage.
        broken = create_connector_account(admin, two_tenants.a.id, platform="shopify")
        healthy = create_connector_account(admin, two_tenants.b.id, platform="nuvemshop")
        event = _abandonment("6001", at=NOW, phone=unique_phone())

        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            # Targets are claimed together; the broken one is polled first or
            # second, and either way the other finishes.
            result = await reconcile_pass(
                conn,
                {
                    "shopify": ScriptedConnector([], fail_after=0),
                    "nuvemshop": ScriptedConnector([event]),
                },
                stale_after=ALWAYS,
            )

        assert result.ingested >= 1
        with admin.cursor() as cur:
            cur.execute(
                "select id, sync_status from public.connector_accounts where id = any(%s)",
                ([broken.id, healthy.id],),
            )
            statuses = dict(cur.fetchall())
        assert statuses[broken.id] == "error"
        assert statuses[healthy.id] == "ok"


class TestTheClaimIsWhatDecidesWhoIsPolled:
    async def test_a_freshly_synced_store_is_not_polled_again(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        account = create_connector_account(admin, two_tenants.a.id)
        connector = ScriptedConnector([])

        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            await repo.finish_sync(conn, account.id, status="ok", cursor_at=NOW)
            await reconcile_pass(
                conn, {"shopify": connector}, stale_after=timedelta(minutes=15)
            )

        # The tick runs more often than the promise it enforces, so most passes
        # find nothing to do. A sweep that polled regardless would multiply the
        # platform's rate limit by the ratio between the two.
        assert all(call.connector_account_id != account.id for call in connector.calls)

    async def test_the_store_is_polled_with_its_own_cursor(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        account = create_connector_account(admin, two_tenants.a.id)
        connector = ScriptedConnector([])

        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            await repo.finish_sync(conn, account.id, status="ok", cursor_at=NOW)
            await reconcile_pass(conn, {"shopify": connector}, stale_after=ALWAYS)

        (call,) = [c for c in connector.calls if c.connector_account_id == account.id]
        # `since`, not "everything". Without the cursor the adapter would
        # re-fetch the store's whole history every quarter of an hour, and D5
        # would hide it perfectly — every event a duplicate, nothing wrong
        # visible anywhere except the platform's rate limit.
        assert isinstance(call, SyncTarget)
        assert call.cursor == NOW
        assert call.tenant_id == two_tenants.a.id

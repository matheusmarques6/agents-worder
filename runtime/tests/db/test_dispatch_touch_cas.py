"""S4 — o CAS de gravação: cada guard da escada, revalidado dentro da transação.

D2 in one sentence: the ladder decides in Python, outside every transaction, and
the write revalidates. `internal.dispatch_touch` is the write, and what these
tests hold it to is the property that makes D2 worth the trouble — when a fact
moved between the decision and the insert, **nothing** is written. Not a partial
row, not an outbox item without a message, not a `sent` touch with no send.

Two kinds of test live here and they are not the same strength:

* **guard by guard** — the touch is arranged with a fact already broken and the
  call refuses. This proves the `WHERE` contains the conjunct;
* **the race, staged for real** (`TestTheRaceBetweenDecidingAndWriting`) — a
  spectator holds the touch's row, the write blocks on it having already been
  decided, the fact is injected and committed, the lock is released, and the
  write refuses. This proves the conjunct is evaluated against the world AFTER
  the wait, which is the only version of the claim that matters and the only one
  a test calling the ladder twice cannot make.

The second is why `dispatch_touch` is two statements. PostgreSQL's READ
COMMITTED re-check re-evaluates a blocked UPDATE's qual against the newly
committed row but keeps the command's original snapshot for its subqueries — so
a message that landed on ANOTHER table while we waited would be invisible, and a
single clever UPDATE would send the touch. Taking the row with `FOR UPDATE`
first, as its own statement, is what gives the CAS a fresh snapshot.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agents_runtime.dispatch.ladder import (
    FUNNEL_COOLDOWN,
    PROACTIVE_WINDOW,
    TIER_PAUSE_FRACTION,
)
from tests.db.conftest import TwoTenants
from tests.db.factories import Thread, create_connector_account, create_message, create_thread
from tests.db.factories_e3 import (
    create_funnel,
    create_order,
    create_scheduled_touch,
    create_suppression,
)
from tests.support.lock_race import wait_until_blocked

pytestmark = pytest.mark.db

PAYLOAD = {"text": "Vi que ficou algo no carrinho.", "generated": False}

#: The event is 60s old by default, so "an inbound message that exists now" is
#: newer than it — which is exactly the staleness case, and exactly why the
#: happy path arranges no inbound message at all.
EVENT_AGE = 60


@dataclass(frozen=True)
class World:
    tenant_id: uuid.UUID
    thread: Thread
    funnel_id: uuid.UUID
    touch_id: uuid.UUID


@pytest.fixture
def world(admin: psycopg.Connection, two_tenants: TwoTenants) -> World:
    tenant_id = two_tenants.a.id
    thread = create_thread(admin, tenant_id)
    funnel = create_funnel(admin, tenant_id, occasion="cart_abandoned")
    touch_id = create_scheduled_touch(
        admin,
        tenant_id,
        funnel.id,
        thread.contact_id,
        conversation_id=thread.conversation_id,
        due_in_seconds=-60,
        event_age_seconds=EVENT_AGE,
        status="enqueued",
    )
    return World(tenant_id=tenant_id, thread=thread, funnel_id=funnel.id, touch_id=touch_id)


DISPATCH = "select * from internal.dispatch_touch(%s, %s, %s, %s, %s, %s, %s, %s)"


def _arguments(world: World, *, inbound_seq: int = 0, cap: int = 1) -> tuple:
    """The guard values a decision would have carried. The windows come from the
    ladder — never a literal here, or the revalidation would be measuring
    something the rule never used."""
    return (
        world.touch_id,
        inbound_seq,
        cap,
        PROACTIVE_WINDOW,
        FUNNEL_COOLDOWN,
        TIER_PAUSE_FRACTION,
        Jsonb(PAYLOAD),
        f"touch-{world.touch_id}",
    )


def dispatch(conn: psycopg.Connection, world: World, **kwargs) -> tuple:
    return conn.execute(DISPATCH, _arguments(world, **kwargs)).fetchone()


def nothing_was_written(conn: psycopg.Connection, world: World) -> bool:
    """The assertion that matters on every refusal: no send, no history, and the
    touch still where the handler left it.

    Keyed on THIS touch's idempotency key rather than on an empty table: several
    of the guards are arranged by putting another proactive send there, and a
    count of zero would be asserting the arrangement instead of the outcome."""
    sends, messages, status, sent_at, outbox_id = conn.execute(
        """
        select (select count(*) from internal.message_outbox where idempotency_key = %s),
               (select count(*) from public.messages
                 where conversation_id = %s and direction = 'outbound'),
               t.status, t.sent_at, t.outbox_id
          from public.scheduled_touches t where t.id = %s
        """,
        (f"touch-{world.touch_id}", world.thread.conversation_id, world.touch_id),
    ).fetchone()
    return (sends, messages, status, sent_at, outbox_id) == (0, 0, "enqueued", None, None)


def a_proactive_send(conn: psycopg.Connection, world: World, *, kind: str = "funnel_touch") -> None:
    conn.execute(
        """
        insert into internal.message_outbox
            (tenant_id, conversation_id, contact_id, channel_account_id,
             kind, payload, idempotency_key)
        values (%s, %s, %s, %s, %s, '{}'::jsonb, %s)
        """,
        (
            world.tenant_id,
            world.thread.conversation_id,
            world.thread.contact_id,
            world.thread.channel_account_id,
            kind,
            uuid.uuid4().hex,
        ),
    )


class TestTheTouchThatGoesOut:
    def test_a_touch_nothing_objects_to_becomes_a_send(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        status, outbox_id = dispatch(admin, world)

        assert status == "sent"
        assert outbox_id is not None

    def test_the_outbox_row_carries_the_cadence_copy_as_a_funnel_touch(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        _, outbox_id = dispatch(admin, world)

        kind, payload, key = admin.execute(
            "select kind, payload, idempotency_key from internal.message_outbox where id = %s",
            (outbox_id,),
        ).fetchone()

        assert kind == "funnel_touch"
        assert payload == PAYLOAD
        # Derived from the touch: a redelivered job produces the same key, and
        # the outbox's UNIQUE is the second lock on the door.
        assert key == f"touch-{world.touch_id}"

    def test_the_conversation_records_what_the_store_said(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        # Without this row the agent that answers the contact's reply (E2) reads
        # a history in which it spoke first about nothing.
        _, outbox_id = dispatch(admin, world)

        row = admin.execute(
            """
            select direction, seq, author_type, content, outbox_id
              from public.messages where conversation_id = %s
            """,
            (world.thread.conversation_id,),
        ).fetchone()

        assert row == ("outbound", 1, "agent", PAYLOAD, outbox_id)

    def test_the_touch_records_when_it_went_out_and_what_it_became(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        # `sent_at` is what the 72h cooldown measures; `outbox_id` is what keeps
        # the two windows from disagreeing about whether a touch happened.
        _, outbox_id = dispatch(admin, world)

        status, sent_at, linked = admin.execute(
            "select status, sent_at, outbox_id from public.scheduled_touches where id = %s",
            (world.touch_id,),
        ).fetchone()

        assert status == "sent"
        assert sent_at is not None
        assert linked == outbox_id

    def test_a_second_call_finds_the_touch_already_finished(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        # A redelivered job is normal (VT expiry, a crash after commit). The
        # second attempt is a non-event, not a second send.
        dispatch(admin, world)

        assert dispatch(admin, world)[0] == "gone"
        assert (
            admin.execute(
                "select count(*) from internal.message_outbox where tenant_id = %s",
                (world.tenant_id,),
            ).fetchone()[0]
            == 1
        )


class TestEveryGuardIsRevalidated:
    def test_a_suppression_that_appeared_stops_the_send(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        # RF-033: the list is checked before EVERY proactive send — including
        # the milliseconds after the ladder already checked it.
        create_suppression(admin, world.tenant_id, world.thread.contact_id)

        assert dispatch(admin, world)[0] == "conflict"
        assert nothing_was_written(admin, world)

    def test_an_order_paid_in_the_meantime_stops_the_send(
        self, admin: psycopg.Connection, world: World, two_tenants: TwoTenants
    ) -> None:
        account = create_connector_account(admin, world.tenant_id)
        order_id = create_order(admin, world.tenant_id, account.id, financial_status="paid")
        admin.execute(
            "update public.scheduled_touches set order_id = %s where id = %s",
            (order_id, world.touch_id),
        )

        assert dispatch(admin, world)[0] == "conflict"
        assert nothing_was_written(admin, world)

    def test_an_unpaid_order_is_not_an_objection(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        account = create_connector_account(admin, world.tenant_id)
        order_id = create_order(admin, world.tenant_id, account.id, financial_status="pending")
        admin.execute(
            "update public.scheduled_touches set order_id = %s where id = %s",
            (order_id, world.touch_id),
        )

        assert dispatch(admin, world)[0] == "sent"

    def test_a_bumped_inbound_counter_stops_the_send(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        # The atomic counter, the same device as the central invariant's CAS in
        # E1: ingestion bumped it, the equality fails, and the draft dies.
        admin.execute(
            "update public.conversations set next_inbound_seq = 1 where id = %s",
            (world.thread.conversation_id,),
        )

        assert dispatch(admin, world, inbound_seq=0)[0] == "conflict"
        assert nothing_was_written(admin, world)

    def test_an_inbound_message_newer_than_the_event_stops_the_send(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        # The same fact by the name the RULE is written in. The counter catches
        # an ingestion mid-flight; this catches a message whose row exists.
        create_message(admin, world.tenant_id, world.thread, seq=1)

        assert dispatch(admin, world)[0] == "conflict"
        assert nothing_was_written(admin, world)

    def test_the_contacts_own_outbound_history_is_not_staleness(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        # The conjunct filters `direction = 'inbound'`, and it has to: what the
        # store already said is not the contact answering.
        create_message(admin, world.tenant_id, world.thread, direction="outbound", seq=1)
        admin.execute(
            "update public.conversations set next_outbound_seq = 1 where id = %s",
            (world.thread.conversation_id,),
        )

        assert dispatch(admin, world)[0] == "sent"

    def test_a_touch_that_already_went_out_today_stops_the_send(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        # RF-034, counted over the outbox because it sums ALL origins.
        a_proactive_send(admin, world)

        assert dispatch(admin, world, cap=1)[0] == "conflict"
        assert nothing_was_written(admin, world)

    def test_a_follow_up_counts_towards_the_same_daily_limit(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        # "All origins" is the point: the limit protects the contact, not the
        # funnel, so a follow-up spends the same allowance.
        a_proactive_send(admin, world, kind="followup")

        assert dispatch(admin, world, cap=1)[0] == "conflict"

    def test_a_reply_does_not_count_towards_the_daily_limit(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        # Reactive messages are never rate limited (RF-034); they never reach
        # the ladder, and they must not spend the proactive allowance either.
        a_proactive_send(admin, world, kind="reply")

        assert dispatch(admin, world, cap=1)[0] == "sent"

    def test_a_tenant_the_admin_raised_may_send_a_second_touch(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        a_proactive_send(admin, world)

        assert dispatch(admin, world, cap=2)[0] == "sent"

    def test_a_touch_from_another_funnel_within_the_cooldown_stops_the_send(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        other = create_funnel(admin, world.tenant_id, occasion="pix_pending")
        create_scheduled_touch(
            admin,
            world.tenant_id,
            other.id,
            world.thread.contact_id,
            status="sent",
            sent_ago_seconds=3600,
        )

        assert dispatch(admin, world)[0] == "conflict"
        assert nothing_was_written(admin, world)

    def test_a_touch_of_the_same_funnel_is_not_a_cooldown(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        # Inside one funnel the spacing is the funnel's own cadence — RF-034 is
        # explicit that there is no per-funnel touch cap.
        create_scheduled_touch(
            admin,
            world.tenant_id,
            world.funnel_id,
            world.thread.contact_id,
            touch_number=9,
            status="sent",
            sent_ago_seconds=3600,
        )

        assert dispatch(admin, world, cap=2)[0] == "sent"

    def test_a_number_at_eighty_percent_of_its_tier_stops_the_send(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        admin.execute(
            "update public.channels_accounts set meta_tier = 10, tier_usage_24h = 8 where id = %s",
            (world.thread.channel_account_id,),
        )

        assert dispatch(admin, world)[0] == "conflict"
        assert nothing_was_written(admin, world)

    def test_below_the_pause_fraction_the_touch_goes_out(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        admin.execute(
            "update public.channels_accounts set meta_tier = 10, tier_usage_24h = 7 where id = %s",
            (world.thread.channel_account_id,),
        )

        assert dispatch(admin, world)[0] == "sent"

    def test_a_number_with_no_tier_is_not_paused_by_this_rung(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        # NULL tier is Evolution, where the ceilings are the sender's anti-ban
        # ones instead (D10).
        assert dispatch(admin, world)[0] == "sent"


class TestWhatIsNotAGuard:
    def test_a_touch_nobody_claimed_is_gone_not_conflicted(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        # `gone` and `conflict` mean different things to the handler: one is
        # somebody else finishing the work, the other is a fact that moved and
        # has to be named. Collapsing them would let the handler cancel a touch
        # that `order_paid` already cancelled, overwriting the true reason.
        admin.execute(
            "update public.scheduled_touches set status = 'pending' where id = %s",
            (world.touch_id,),
        )

        assert dispatch(admin, world)[0] == "gone"

    def test_a_touch_a_paid_order_already_cancelled_is_gone(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        admin.execute(
            "update public.scheduled_touches"
            "   set status = 'cancelled', cancel_reason = 'stale_order_paid' where id = %s",
            (world.touch_id,),
        )

        assert dispatch(admin, world)[0] == "gone"
        assert (
            admin.execute(
                "select cancel_reason from public.scheduled_touches where id = %s",
                (world.touch_id,),
            ).fetchone()[0]
            == "stale_order_paid"
        )

    def test_a_touch_without_a_conversation_has_no_way_out(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        # Neither a guard nor a cancellation: an arrangement that should be
        # impossible, and the caller raises so it reaches a human.
        admin.execute(
            "update public.scheduled_touches set conversation_id = null where id = %s",
            (world.touch_id,),
        )

        assert dispatch(admin, world)[0] == "no_channel"


class TestTheRaceBetweenDecidingAndWriting:
    """The claim of D2, staged rather than simulated.

    The write is caught mid-flight — decided, not yet committed — by a lock a
    spectator holds on the touch's own row. The fact is injected and committed
    while it waits, and the question is what the CAS sees when it wakes up.
    """

    async def _staged(
        self, dsn: str, world: World, inject
    ) -> tuple[str, uuid.UUID | None]:
        holder = await psycopg.AsyncConnection.connect(dsn)
        watcher = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
        writer = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
        try:
            await holder.execute(
                "select id from public.scheduled_touches where id = %s for update",
                (world.touch_id,),
            )

            writing = asyncio.ensure_future(writer.execute(DISPATCH, _arguments(world)))
            # Not a sleep and a hope: the server itself says a backend is
            # waiting on a lock inside `dispatch_touch`. The decision has
            # happened; the write has not.
            await wait_until_blocked(watcher, needle="dispatch_touch")

            await inject(holder)
            await holder.commit()

            cursor = await asyncio.wait_for(writing, 15)
            return await cursor.fetchone()
        finally:
            await holder.close()
            await watcher.close()
            await writer.close()

    async def test_a_message_that_lands_while_the_write_waits_stops_the_send(
        self, dsn: str, admin: psycopg.Connection, world: World
    ) -> None:
        async def a_contact_writes(conn: psycopg.AsyncConnection) -> None:
            # Exactly what ingestion does: the counter and the row, together.
            await conn.execute(
                "update public.conversations set next_inbound_seq = next_inbound_seq + 1"
                " where id = %s",
                (world.thread.conversation_id,),
            )
            await conn.execute(
                """
                insert into public.messages
                    (tenant_id, conversation_id, direction, seq, channel, author_type, content)
                values (%s, %s, 'inbound', 1, 'whatsapp_cloud', 'contact', '{"text": "oi"}')
                """,
                (world.tenant_id, world.thread.conversation_id),
            )

        status, outbox_id = await self._staged(dsn, world, a_contact_writes)

        assert status == "conflict"
        assert outbox_id is None
        assert nothing_was_written(admin, world)

    async def test_a_payment_that_lands_while_the_write_waits_stops_the_send(
        self, dsn: str, admin: psycopg.Connection, world: World
    ) -> None:
        account = create_connector_account(admin, world.tenant_id)
        order_id = create_order(admin, world.tenant_id, account.id, financial_status="pending")
        admin.execute(
            "update public.scheduled_touches set order_id = %s where id = %s",
            (order_id, world.touch_id),
        )

        async def the_order_is_paid(conn: psycopg.AsyncConnection) -> None:
            await conn.execute(
                "update public.orders set financial_status = 'paid' where id = %s", (order_id,)
            )

        status, _ = await self._staged(dsn, world, the_order_is_paid)

        assert status == "conflict"
        assert nothing_was_written(admin, world)

    async def test_a_block_that_lands_while_the_write_waits_stops_the_send(
        self, dsn: str, admin: psycopg.Connection, world: World
    ) -> None:
        async def the_contact_blocks_us(conn: psycopg.AsyncConnection) -> None:
            await conn.execute(
                """
                insert into public.suppression_list (tenant_id, contact_id, reason, created_by)
                values (%s, %s, 'explicit_block', 'agent')
                """,
                (world.tenant_id, world.thread.contact_id),
            )

        status, _ = await self._staged(dsn, world, the_contact_blocks_us)

        assert status == "conflict"
        assert nothing_was_written(admin, world)

    async def test_a_touch_that_goes_out_while_the_write_waits_spends_the_allowance(
        self, dsn: str, admin: psycopg.Connection, world: World
    ) -> None:
        # The window that is a COUNT rather than a boolean, and the one a single
        # clever UPDATE would get wrong most quietly.
        async def another_touch_goes_out(conn: psycopg.AsyncConnection) -> None:
            await conn.execute(
                """
                insert into internal.message_outbox
                    (tenant_id, conversation_id, contact_id, channel_account_id,
                     kind, payload, idempotency_key)
                values (%s, %s, %s, %s, 'funnel_touch', '{}'::jsonb, %s)
                """,
                (
                    world.tenant_id,
                    world.thread.conversation_id,
                    world.thread.contact_id,
                    world.thread.channel_account_id,
                    uuid.uuid4().hex,
                ),
            )

        status, _ = await self._staged(dsn, world, another_touch_goes_out)

        assert status == "conflict"
        assert nothing_was_written(admin, world)

    async def test_the_same_wait_with_nothing_injected_still_sends(
        self, dsn: str, admin: psycopg.Connection, world: World
    ) -> None:
        """The control the other four need to mean anything.

        Same lock, same block, same wakeup — and the touch goes out. Without
        this, every refusal above could be an artefact of being blocked at all
        rather than of the fact that was injected."""

        async def nothing_happens(conn: psycopg.AsyncConnection) -> None:
            return None

        status, outbox_id = await self._staged(dsn, world, nothing_happens)

        assert status == "sent"
        assert outbox_id is not None


class TestTheWindowsComeFromTheLadder:
    """Dívida (c) do S3, fechada por construção: o módulo declara a janela e
    quem conta recebe a MESMA janela como parâmetro.

    The hazard being closed is specific: `PROACTIVE_WINDOW` names 24 hours in
    Python while a query somewhere else counts `interval '24 hours'` of its own.
    Both would be right today and only one of them would change.
    """

    def test_the_daily_count_ends_exactly_where_the_constant_says(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        a_proactive_send(admin, world)
        admin.execute(
            "update internal.message_outbox set created_at = now() - %s + interval '1 minute'"
            " where tenant_id = %s",
            (PROACTIVE_WINDOW, world.tenant_id),
        )

        # One minute inside the window the ladder names: it counts.
        assert dispatch(admin, world, cap=1)[0] == "conflict"

    def test_a_send_older_than_the_window_no_longer_counts(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        a_proactive_send(admin, world)
        admin.execute(
            "update internal.message_outbox set created_at = now() - %s - interval '1 minute'"
            " where tenant_id = %s",
            (PROACTIVE_WINDOW, world.tenant_id),
        )

        assert dispatch(admin, world, cap=1)[0] == "sent"

    def test_the_cooldown_ends_exactly_where_the_constant_says(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        other = create_funnel(admin, world.tenant_id, occasion="pix_pending")
        create_scheduled_touch(
            admin,
            world.tenant_id,
            other.id,
            world.thread.contact_id,
            status="sent",
            sent_ago_seconds=int(FUNNEL_COOLDOWN.total_seconds()) - 60,
        )

        assert dispatch(admin, world)[0] == "conflict"

    def test_a_funnel_older_than_the_cooldown_no_longer_blocks(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        other = create_funnel(admin, world.tenant_id, occasion="pix_pending")
        create_scheduled_touch(
            admin,
            world.tenant_id,
            other.id,
            world.thread.contact_id,
            status="sent",
            sent_ago_seconds=int(FUNNEL_COOLDOWN.total_seconds()) + 60,
        )

        assert dispatch(admin, world)[0] == "sent"

    def test_the_window_is_a_parameter_and_not_a_literal_in_the_function(
        self, admin: psycopg.Connection
    ) -> None:
        # The structural half of the same claim: a literal here would be a
        # second copy of a canonical number, free to drift from the constant the
        # rule is written against, and every test above would still pass.
        body = admin.execute(
            "select prosrc from pg_proc where proname = 'dispatch_touch'"
        ).fetchone()[0]

        assert "interval '24 hours'" not in body
        assert "interval '72 hours'" not in body
        assert "p_proactive_window" in body
        assert "p_funnel_cooldown" in body

    def test_a_shorter_window_handed_in_is_the_window_enforced(
        self, admin: psycopg.Connection, world: World
    ) -> None:
        # Proof that the parameter is load-bearing rather than decorative: the
        # same facts, a window of one second, and the send that was blocked goes
        # out. If the function counted its own 24 hours this would still refuse.
        a_proactive_send(admin, world)
        admin.execute(
            "update internal.message_outbox set created_at = now() - interval '1 hour'"
            " where tenant_id = %s",
            (world.tenant_id,),
        )

        blocked = admin.execute(
            DISPATCH, _arguments(world)
        ).fetchone()[0]
        allowed = admin.execute(
            DISPATCH,
            (
                world.touch_id,
                0,
                1,
                timedelta(seconds=1),
                FUNNEL_COOLDOWN,
                TIER_PAUSE_FRACTION,
                Jsonb(PAYLOAD),
                f"touch-{world.touch_id}",
            ),
        ).fetchone()[0]

        assert (blocked, allowed) == ("conflict", "sent")

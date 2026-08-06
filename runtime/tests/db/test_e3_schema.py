"""E3 · S2 — the schema slice of the milestone, and what it makes impossible.

What is asserted here is only what no later step can re-decide cheaply:

· **The platform ceiling of RF-034 is a CHECK** (D1). Four is the absolute
  maximum of proactive touches per contact per 24h, and a value of 5 has to be
  an error from the database — not a rule some service remembers to apply.

· **`scheduled_touches.cancel_reason` carries the ladder's vocabulary.** The
  dictionary's four values could not hold the nine reasons `dispatch/ladder.py`
  produces, and flattening them would erase exactly the metric S11 asks for
  ("touches cancelled BY REASON"). The list is imported from the ladder rather
  than retyped, so the two cannot drift.

· **The two windows of the ladder have a source in the schema.** S4 has to turn
  `Guards` into a `WHERE`; a column it cannot find is a guess it would have to
  invent. The 24h count of RF-034 sums ALL origins and therefore reads the
  outbox — the one table every proactive send passes through; the 72h cooldown
  is between DIFFERENT funnels, and funnel identity exists only in
  `scheduled_touches`.

· **Recovered revenue survives the purge** (D8). `messages` has a rolling TTL of
  12 months and the conversation can be deleted; the conversion row may not go
  with it, because recomputing it afterwards is impossible.
"""

import re

import psycopg
import pytest

from agents_runtime.dispatch.ladder import DENIAL_REASONS
from tests.db.conftest import TwoTenants
from tests.db.factories import (
    create_connector_account,
    create_contact,
    create_message,
    create_outbox_item,
    create_thread,
    unique_id,
)
from tests.db.factories_e3 import (
    create_funnel,
    create_funnel_conversion,
    create_order,
    create_scheduled_touch,
    create_suppression,
)

# The ladder's nine denial reasons, plus the one cancellation the ladder cannot
# produce: an operator cancelling a touch by hand.
EXPECTED_CANCEL_REASONS = set(DENIAL_REASONS) | {"manual"}


class TestThePlatformCeilingIsACheck:
    def test_a_tenant_starts_at_one_touch_per_contact_per_day(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        value = admin.execute(
            "select proactive_max_per_contact_24h from public.tenants where id = %s",
            (two_tenants.a.id,),
        ).fetchone()[0]

        assert value == 1

    def test_the_attribution_window_starts_at_twenty_four_hours(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        value = admin.execute(
            "select attribution_window_hours from public.tenants where id = %s",
            (two_tenants.a.id,),
        ).fetchone()[0]

        assert value == 24

    def test_a_cap_of_five_is_rejected_by_the_database(self, admin: psycopg.Connection) -> None:
        # The ceiling of RF-034 is 4. Not a convention, not a service rule: a
        # constraint, so that no write path anywhere can exceed it.
        with pytest.raises(psycopg.errors.CheckViolation):
            admin.execute(
                """
                insert into public.tenants (name, proactive_max_per_contact_24h)
                values ('teto furado', 5)
                """
            )

    def test_a_cap_of_zero_is_rejected(self, admin: psycopg.Connection) -> None:
        # Zero would not be "tighter", it would be a funnel that never speaks —
        # switching the product off through a number nobody meant as a switch.
        with pytest.raises(psycopg.errors.CheckViolation):
            admin.execute(
                """
                insert into public.tenants (name, proactive_max_per_contact_24h)
                values ('teto zerado', 0)
                """
            )

    def test_the_whole_allowed_range_is_accepted(self, admin: psycopg.Connection) -> None:
        for value in (1, 2, 3, 4):
            admin.execute(
                """
                insert into public.tenants (name, proactive_max_per_contact_24h)
                values (%s, %s)
                """,
                (unique_id("cap"), value),
            )


class TestTheCancelReasonSpeaksTheLadderVocabulary:
    @pytest.fixture
    def touch_args(self, admin: psycopg.Connection, two_tenants: TwoTenants) -> tuple:
        funnel = create_funnel(admin, two_tenants.a.id)
        contact = create_contact(admin, two_tenants.a.id)
        return (two_tenants.a.id, funnel.id, contact)

    @pytest.mark.parametrize("reason", sorted(EXPECTED_CANCEL_REASONS))
    def test_every_reason_the_ladder_can_produce_is_storable(
        self, admin: psycopg.Connection, touch_args: tuple, reason: str
    ) -> None:
        touch_id = create_scheduled_touch(
            admin, *touch_args, status="cancelled", cancel_reason=reason
        )

        assert touch_id is not None

    def test_the_old_four_value_vocabulary_no_longer_fits(
        self, admin: psycopg.Connection, touch_args: tuple
    ) -> None:
        # `replied` and `paid` were the dictionary's words for two of the
        # ladder's reasons. Keeping them as synonyms would split the S11 metric
        # in two, so they are gone rather than aliased.
        for retired in ("replied", "paid", "suppressed"):
            with pytest.raises(psycopg.errors.CheckViolation):
                create_scheduled_touch(
                    admin, *touch_args, status="cancelled", cancel_reason=retired
                )

    def test_the_check_holds_exactly_the_ladder_reasons_plus_manual(
        self, admin: psycopg.Connection
    ) -> None:
        # Read the constraint itself: a reason added to the schema and not to
        # the ladder is a value nothing can ever write, and the reverse is a
        # cancellation that fails at 3am.
        definition = admin.execute(
            """
            select pg_get_constraintdef(oid)
              from pg_constraint
             where conrelid = 'public.scheduled_touches'::regclass
               and conname = 'scheduled_touches_cancel_reason_check'
            """
        ).fetchone()[0]

        allowed = set(re.findall(r"'([a-z0-9_]+)'::text", definition))

        assert allowed == EXPECTED_CANCEL_REASONS

    def test_a_cancelled_touch_without_a_reason_is_rejected(
        self, admin: psycopg.Connection, touch_args: tuple
    ) -> None:
        # A cancellation with no reason is the one row that destroys the metric
        # it belongs to: the total still moves, the diagnosis disappears.
        with pytest.raises(psycopg.errors.CheckViolation):
            create_scheduled_touch(admin, *touch_args, status="cancelled")

    def test_a_pending_touch_may_not_carry_a_reason(
        self, admin: psycopg.Connection, touch_args: tuple
    ) -> None:
        with pytest.raises(psycopg.errors.CheckViolation):
            create_scheduled_touch(admin, *touch_args, cancel_reason="manual")


class TestASentTouchSaysWhenItWentOut:
    @pytest.fixture
    def touch_args(self, admin: psycopg.Connection, two_tenants: TwoTenants) -> tuple:
        funnel = create_funnel(admin, two_tenants.a.id)
        contact = create_contact(admin, two_tenants.a.id)
        return (two_tenants.a.id, funnel.id, contact)

    def test_a_sent_touch_without_a_timestamp_is_rejected(
        self, admin: psycopg.Connection, touch_args: tuple
    ) -> None:
        # Without `sent_at` the 72h cooldown between funnels has nothing to
        # measure, and the ladder would wave every second funnel through.
        with pytest.raises(psycopg.errors.CheckViolation):
            create_scheduled_touch(admin, *touch_args, status="sent")

    def test_a_pending_touch_may_not_claim_to_have_been_sent(
        self, admin: psycopg.Connection, touch_args: tuple
    ) -> None:
        with pytest.raises(psycopg.errors.CheckViolation):
            create_scheduled_touch(admin, *touch_args, sent_ago_seconds=10)

    def test_the_touch_points_at_the_outbox_row_it_produced(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # The link is what keeps the two windows from disagreeing about whether
        # a touch happened: one send, one row in each table.
        thread = create_thread(admin, two_tenants.a.id)
        funnel = create_funnel(admin, two_tenants.a.id)
        outbox_id = create_outbox_item(admin, two_tenants.a.id, thread)
        touch_id = create_scheduled_touch(
            admin,
            two_tenants.a.id,
            funnel.id,
            thread.contact_id,
            conversation_id=thread.conversation_id,
            status="sent",
            sent_ago_seconds=30,
            outbox_id=outbox_id,
        )

        linked = admin.execute(
            "select outbox_id from public.scheduled_touches where id = %s", (touch_id,)
        ).fetchone()[0]

        assert linked == outbox_id


class TestTheTwoWindowsHaveASource:
    """The queries S4 will write, run against the schema this migration ships."""

    def test_the_24h_count_sums_every_proactive_origin(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # RF-034 counts ALL origins, so the source is the outbox — the single
        # door every proactive send goes through — and not `scheduled_touches`,
        # which only knows about funnels.
        thread = create_thread(admin, two_tenants.a.id)
        for kind in ("funnel_touch", "followup", "reply"):
            admin.execute(
                """
                insert into internal.message_outbox
                    (tenant_id, conversation_id, contact_id, channel_account_id,
                     kind, payload, idempotency_key)
                values (%s, %s, %s, %s, %s, '{}'::jsonb, %s)
                """,
                (
                    two_tenants.a.id,
                    thread.conversation_id,
                    thread.contact_id,
                    thread.channel_account_id,
                    kind,
                    unique_id("idem"),
                ),
            )

        counted = admin.execute(
            """
            select count(*)
              from internal.message_outbox
             where contact_id = %s
               and kind in ('funnel_touch', 'followup')
               and created_at > now() - interval '24 hours'
            """,
            (thread.contact_id,),
        ).fetchone()[0]

        # The reply does not count: reactive messages are never rate limited.
        assert counted == 2

    def test_the_72h_cooldown_only_sees_a_different_funnel(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        contact = create_contact(admin, two_tenants.a.id)
        cart = create_funnel(admin, two_tenants.a.id, occasion="cart_abandoned")
        pix = create_funnel(admin, two_tenants.a.id, occasion="pix_pending")
        create_scheduled_touch(
            admin, two_tenants.a.id, cart.id, contact, status="sent", sent_ago_seconds=3600
        )
        create_scheduled_touch(
            admin, two_tenants.a.id, pix.id, contact, status="sent", sent_ago_seconds=3600
        )

        # Asked from inside the cart funnel: only the PIX touch is a cooldown.
        last = admin.execute(
            """
            select max(sent_at)
              from public.scheduled_touches
             where contact_id = %s
               and funnel_id <> %s
               and status = 'sent'
               and sent_at > now() - interval '72 hours'
            """,
            (contact, cart.id),
        ).fetchone()[0]

        assert last is not None

    def test_a_touch_of_the_same_funnel_is_not_a_cooldown(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # Inside one funnel the spacing is the funnel's own cadence — there is
        # no per-funnel touch cap (RF-034).
        contact = create_contact(admin, two_tenants.a.id)
        cart = create_funnel(admin, two_tenants.a.id, occasion="cart_abandoned")
        create_scheduled_touch(
            admin, two_tenants.a.id, cart.id, contact, status="sent", sent_ago_seconds=3600
        )

        last = admin.execute(
            """
            select max(sent_at)
              from public.scheduled_touches
             where contact_id = %s
               and funnel_id <> %s
               and status = 'sent'
               and sent_at > now() - interval '72 hours'
            """,
            (contact, cart.id),
        ).fetchone()[0]

        assert last is None

    def test_the_dispatcher_scan_has_an_index(self, admin: psycopg.Connection) -> None:
        # `claim_due_touches` runs every minute over the whole table; without
        # this index it is a sequential scan of every touch ever scheduled.
        indexes = admin.execute(
            """
            select indexdef from pg_indexes
             where schemaname = 'public' and tablename = 'scheduled_touches'
            """
        ).fetchall()

        assert any("status" in row[0] and "due_at" in row[0] for row in indexes)

    def test_the_staleness_check_has_an_instant_to_compare_against(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # `event_at` is when the abandonment happened, not when the touch was
        # scheduled. A default would silently substitute one for the other and
        # break exactly the late-event case staleness exists for.
        thread = create_thread(admin, two_tenants.a.id)
        funnel = create_funnel(admin, two_tenants.a.id)
        create_message(admin, two_tenants.a.id, thread, seq=1)
        touch_id = create_scheduled_touch(
            admin,
            two_tenants.a.id,
            funnel.id,
            thread.contact_id,
            conversation_id=thread.conversation_id,
            event_age_seconds=600,
        )

        newer = admin.execute(
            """
            select exists (
                select 1
                  from public.messages m
                  join public.scheduled_touches t on t.conversation_id = m.conversation_id
                 where t.id = %s and m.direction = 'inbound' and m.created_at > t.event_at
            )
            """,
            (touch_id,),
        ).fetchone()[0]

        assert newer is True


class TestOneEnabledFunnelPerOccasion:
    def test_a_second_enabled_funnel_for_the_same_occasion_is_rejected(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # `start_funnel_run` asks "does this occasion have an enabled funnel?".
        # Two answers is not a configuration, it is a coin toss.
        create_funnel(admin, two_tenants.a.id, occasion="pix_pending")

        with pytest.raises(psycopg.errors.UniqueViolation):
            create_funnel(admin, two_tenants.a.id, occasion="pix_pending")

    def test_disabled_funnels_pile_up_freely(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        create_funnel(admin, two_tenants.a.id, occasion="pix_pending")
        create_funnel(admin, two_tenants.a.id, occasion="pix_pending", enabled=False)
        create_funnel(admin, two_tenants.a.id, occasion="pix_pending", enabled=False)

        total = admin.execute(
            "select count(*) from public.funnels where tenant_id = %s", (two_tenants.a.id,)
        ).fetchone()[0]

        assert total == 3

    def test_each_tenant_keeps_its_own_funnel(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        create_funnel(admin, two_tenants.a.id, occasion="pix_pending")
        other = create_funnel(admin, two_tenants.b.id, occasion="pix_pending")

        assert other.id is not None


class TestTheOrderMirror:
    def test_an_unmapped_platform_status_is_rejected(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # Integrity note 3: `financial_status = 'paid'` is the signal that
        # cancels a funnel. An unmapped platform word would not fail — it would
        # simply never equal 'paid', and the merchant would charge a contact who
        # already paid. The connector adapter normalises; the CHECK is what
        # makes forgetting loud.
        account = create_connector_account(admin, two_tenants.a.id)

        with pytest.raises(psycopg.errors.CheckViolation):
            create_order(admin, two_tenants.a.id, account.id, financial_status="Paid")

    def test_the_same_order_cannot_be_mirrored_twice(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        account = create_connector_account(admin, two_tenants.a.id)
        create_order(admin, two_tenants.a.id, account.id, external_id="1042")

        with pytest.raises(psycopg.errors.UniqueViolation):
            create_order(admin, two_tenants.a.id, account.id, external_id="1042")

    def test_two_stores_may_share_an_order_number(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # Platforms hand out per-store sequential ids: "1042" exists in every
        # store. The uniqueness is per connector account, never global.
        a_account = create_connector_account(admin, two_tenants.a.id)
        b_account = create_connector_account(admin, two_tenants.b.id)
        create_order(admin, two_tenants.a.id, a_account.id, external_id="1042")
        other = create_order(admin, two_tenants.b.id, b_account.id, external_id="1042")

        assert other is not None

    def test_a_contact_can_be_linked_to_the_mirrored_customer(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # The omission of the E1 slice, closed: `contacts.customer_id` is what
        # `get_customer_context` needs to have a consumer at all.
        from tests.db.factories_e3 import create_customer

        account = create_connector_account(admin, two_tenants.a.id)
        customer_id = create_customer(admin, two_tenants.a.id, account.id)
        contact_id = create_contact(admin, two_tenants.a.id)

        admin.execute(
            "update public.contacts set customer_id = %s where id = %s",
            (customer_id, contact_id),
        )
        linked = admin.execute(
            "select customer_id from public.contacts where id = %s", (contact_id,)
        ).fetchone()[0]

        assert linked == customer_id


class TestSuppressionIsOnePerContact:
    def test_a_contact_is_suppressed_once(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        contact = create_contact(admin, two_tenants.a.id)
        create_suppression(admin, two_tenants.a.id, contact)

        with pytest.raises(psycopg.errors.UniqueViolation):
            create_suppression(admin, two_tenants.a.id, contact, reason="intent_optout")


class TestRecoveredRevenueOutlivesTheConversation:
    def test_an_order_is_credited_at_most_once(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        account = create_connector_account(admin, two_tenants.a.id)
        order_id = create_order(admin, two_tenants.a.id, account.id, financial_status="paid")
        funnel = create_funnel(admin, two_tenants.a.id)
        create_funnel_conversion(
            admin, two_tenants.a.id, funnel_id=funnel.id, order_id=order_id
        )

        # Two funnels claiming the same payment would double the only number
        # the merchant bought the product for.
        with pytest.raises(psycopg.errors.UniqueViolation):
            create_funnel_conversion(
                admin, two_tenants.a.id, funnel_id=funnel.id, order_id=order_id
            )

    def test_the_conversion_survives_the_purge_of_the_conversation(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # D8 in one test: `messages` has a rolling TTL of 12 months, so revenue
        # attribution cannot be a query over them. Deleting the conversation
        # takes the messages with it and must leave the money behind.
        account = create_connector_account(admin, two_tenants.a.id)
        thread = create_thread(admin, two_tenants.a.id)
        create_message(admin, two_tenants.a.id, thread, seq=1)
        funnel = create_funnel(admin, two_tenants.a.id)
        order_id = create_order(admin, two_tenants.a.id, account.id, financial_status="paid")
        touch_id = create_scheduled_touch(
            admin,
            two_tenants.a.id,
            funnel.id,
            thread.contact_id,
            conversation_id=thread.conversation_id,
            order_id=order_id,
            status="sent",
            sent_ago_seconds=60,
        )
        conversion_id = create_funnel_conversion(
            admin,
            two_tenants.a.id,
            funnel_id=funnel.id,
            contact_id=thread.contact_id,
            touch_id=touch_id,
            order_id=order_id,
        )

        admin.execute(
            "delete from public.conversations where id = %s", (thread.conversation_id,)
        )

        row = admin.execute(
            "select amount, currency from public.funnel_conversions where id = %s",
            (conversion_id,),
        ).fetchone()

        assert row is not None
        assert str(row[0]) == "199.90"
        assert row[1] == "BRL"

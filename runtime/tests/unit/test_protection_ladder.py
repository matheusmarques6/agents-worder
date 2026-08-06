"""The protection ladder — which proactive touches are allowed to exist.

S1 of E3, written before any touch exists that it could block: the order is
deliberate. Every rule here is a reason a real contact would have to be
protected from a store that talks first.

Two things these tests care about beyond the rules themselves:

* **Precedence is an assertion, not a convention.** A suppressed contact
  reported as "hit the rate limit" sends the operator to look at the wrong
  place — and worse, suggests the touch would go out tomorrow. Every pairwise
  step of `supressão → quota → staleness → limites` is pinned below.
* **The guards are the point.** S4 writes to the outbox through a
  compare-and-set whose `WHERE` revalidates each guard inside the same short
  transaction (D2). A decision that did not carry its facts would force the
  rule to be written a second time, in SQL.

Reasons are asserted as literal strings rather than by importing the module's
own vocabulary — the S4 lesson of E2: a test that cites the constant it
verifies inverts along with it and proves nothing.
"""

from datetime import UTC, datetime, timedelta

import pytest

from agents_runtime.dispatch.ladder import ProactiveTouch, decide
from agents_runtime.quota import Allowance
from tests.support.clock import FrozenClock

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)

#: Old enough that RF-032's staleness check applies — which is the normal case:
#: a scheduled touch is due hours after the event that justified it.
EVENT_AT = NOW - timedelta(hours=6)


def _touch(**overrides) -> ProactiveTouch:
    """A touch nothing objects to, so each test introduces exactly one fact."""
    facts: dict[str, object] = {
        "event_at": EVENT_AT,
        "inbound_seq": 7,
    }
    facts.update(overrides)
    return ProactiveTouch(**facts)  # type: ignore[arg-type]


def _clock(at: datetime = NOW) -> FrozenClock:
    return FrozenClock(at)


class TestTheHappyPath:
    def test_a_touch_nothing_objects_to_is_allowed(self) -> None:
        decision = decide(_touch(), _clock())

        assert decision.allow is True
        assert decision.reason == "allowed"


class TestSuppression:
    """RF-033. The list is checked before EVERY proactive send, and it is the
    first rung: a contact who asked to be left alone is not a quota problem."""

    def test_an_explicit_block_suppresses_the_touch(self) -> None:
        decision = decide(_touch(suppression_reason="explicit_block"), _clock())

        assert decision.allow is False
        assert decision.reason == "suppressed_block"

    def test_silence_after_three_touches_suppresses_the_touch(self) -> None:
        decision = decide(_touch(suppression_reason="no_response_after_3"), _clock())

        assert decision.allow is False
        assert decision.reason == "suppressed_silence"

    def test_an_expressed_opt_out_suppresses_the_touch(self) -> None:
        decision = decide(_touch(suppression_reason="intent_optout"), _clock())

        assert decision.allow is False
        assert decision.reason == "suppressed_optout"

    def test_a_suppression_reason_the_ladder_does_not_know_still_blocks(self) -> None:
        """Fail closed. A row in `suppression_list` means someone asked not to be
        touched; a reason this code has not learned yet is not permission."""
        decision = decide(_touch(suppression_reason="a_reason_from_the_future"), _clock())

        assert decision.allow is False
        assert decision.reason.startswith("suppressed_")


class TestQuota:
    """RF-073 / D9. Unlimited by default — the rule exists so that the day plans
    exist, the enforcement point is already wired and already tested."""

    def test_an_exhausted_allowance_denies_the_touch(self) -> None:
        decision = decide(
            _touch(quota=Allowance(proactive_touches_remaining=0)),
            _clock(),
        )

        assert decision.allow is False
        assert decision.reason == "quota_exceeded"

    def test_a_remaining_allowance_lets_the_touch_through(self) -> None:
        decision = decide(
            _touch(quota=Allowance(proactive_touches_remaining=1)),
            _clock(),
        )

        assert decision.allow is True


class TestStaleness:
    """RF-032 / arquitetura §5.3: an event older than 5 minutes is re-checked
    against what happened since."""

    def test_a_message_that_arrived_after_the_event_kills_the_touch(self) -> None:
        """D7: the contact answering does not get a trigger in the hot path —
        the ladder is where a live conversation cancels a scheduled touch."""
        decision = decide(_touch(last_inbound_at=EVENT_AT + timedelta(minutes=1)), _clock())

        assert decision.allow is False
        assert decision.reason == "stale_newer_message"

    def test_a_message_from_before_the_event_is_not_staleness(self) -> None:
        decision = decide(_touch(last_inbound_at=EVENT_AT - timedelta(minutes=1)), _clock())

        assert decision.allow is True

    def test_an_order_paid_in_the_meantime_kills_the_touch(self) -> None:
        """Charging someone who already paid is the single worst thing this
        product can do."""
        decision = decide(_touch(order_paid=True), _clock())

        assert decision.allow is False
        assert decision.reason == "stale_order_paid"

    def test_a_fresh_event_is_still_checked_against_a_newer_message(self) -> None:
        """Deliberately stricter than the letter of RF-032, which asks for the
        check on a LATE event (> 5 min): that describes when the check became
        NECESSARY — draining after an outage — not permission to skip a
        protection while the event is fresh. A contact who wrote 30 seconds ago
        is in a live conversation the agent is already answering by reaction;
        a funnel touch on top of that is precisely what annoys the person we
        were trying to recover. Being stricter than the RF only ever suppresses
        a send — it can never create one.

        It stopped being theoretical with S3: the first touch of a funnel is
        born as a `scheduled_touch` already due, so the fresh path IS the normal
        path."""
        fresh = NOW - timedelta(minutes=4)
        decision = decide(
            _touch(event_at=fresh, last_inbound_at=fresh + timedelta(seconds=30)),
            _clock(),
        )

        assert decision.allow is False
        assert decision.reason == "stale_newer_message"

    def test_a_fresh_event_is_still_checked_against_a_paid_order(self) -> None:
        """The other half of the same divergence: the first touch of a funnel is
        fired seconds after the event, and `order_paid` arriving in those
        seconds is a race the ladder must lose towards silence."""
        decision = decide(
            _touch(event_at=NOW - timedelta(minutes=4), order_paid=True),
            _clock(),
        )

        assert decision.allow is False
        assert decision.reason == "stale_order_paid"

    def test_staleness_does_not_depend_on_when_the_question_is_asked(self) -> None:
        """The consequence of dropping the age gate, stated as an invariant: the
        same facts produce the same denial whether the event is four minutes or
        four days old."""
        clock = _clock()
        fresh = decide(
            _touch(event_at=NOW - timedelta(minutes=4), last_inbound_at=NOW - timedelta(minutes=3)),
            clock,
        )
        ancient = decide(
            _touch(event_at=NOW - timedelta(days=4), last_inbound_at=NOW - timedelta(days=3)),
            clock,
        )

        assert fresh.reason == ancient.reason == "stale_newer_message"


class TestRateLimits:
    """RF-034 / arquitetura §5.4."""

    def test_the_default_is_one_proactive_touch_per_contact_per_day(self) -> None:
        decision = decide(_touch(proactive_sends_last_24h=1), _clock())

        assert decision.allow is False
        assert decision.reason == "rate_limit_24h"

    def test_a_tenant_raised_by_the_admin_may_touch_up_to_its_value(self) -> None:
        decision = decide(
            _touch(proactive_sends_last_24h=3, max_proactive_per_24h=4),
            _clock(),
        )

        assert decision.allow is True

    def test_the_platform_ceiling_of_four_wins_over_a_looser_tenant_value(self) -> None:
        """D1 makes the ceiling a CHECK in the database (S2). The ladder clamps
        anyway: the backend never trusts a number it was handed, and a clamp can
        only ever tighten — while the CHECK holds, this line is invisible."""
        decision = decide(
            _touch(proactive_sends_last_24h=4, max_proactive_per_24h=9),
            _clock(),
        )

        assert decision.allow is False
        assert decision.reason == "rate_limit_24h"

    def test_a_touch_from_another_funnel_within_seventy_two_hours_is_denied(self) -> None:
        decision = decide(
            _touch(other_funnel_touch_at=NOW - timedelta(hours=71)),
            _clock(),
        )

        assert decision.allow is False
        assert decision.reason == "funnel_cooldown_72h"

    def test_after_seventy_two_hours_another_funnel_may_touch_again(self) -> None:
        decision = decide(
            _touch(other_funnel_touch_at=NOW - timedelta(hours=72, seconds=1)),
            _clock(),
        )

        assert decision.allow is True

    def test_the_cooldown_is_between_funnels_not_within_one(self) -> None:
        """RF-034 is explicit that there is no per-funnel touch cap: the total
        comes from the funnel's own cadence. Only a DIFFERENT funnel's touch
        starts the 72h clock."""
        decision = decide(_touch(other_funnel_touch_at=None), _clock())

        assert decision.allow is True

    def test_proactives_pause_at_eighty_percent_of_the_meta_tier(self) -> None:
        """RF-035. Reactive replies are never paused — they never reach this
        module at all."""
        decision = decide(_touch(tier_limit_24h=1000, tier_usage_24h=800), _clock())

        assert decision.allow is False
        assert decision.reason == "channel_paused_tier"

    def test_below_eighty_percent_of_the_tier_the_touch_goes_out(self) -> None:
        decision = decide(_touch(tier_limit_24h=1000, tier_usage_24h=799), _clock())

        assert decision.allow is True


class TestPrecedence:
    """`supressão → quota → staleness → limites`, one rung at a time. The wrong
    reason sends the operator to the wrong place: "estourou o limite" reads as
    "it goes out tomorrow", while a blocked contact never gets another touch."""

    def test_a_suppressed_contact_is_never_reported_as_anything_else(self) -> None:
        decision = decide(
            _touch(
                suppression_reason="explicit_block",
                quota=Allowance(proactive_touches_remaining=0),
                last_inbound_at=NOW - timedelta(minutes=1),
                order_paid=True,
                proactive_sends_last_24h=9,
                other_funnel_touch_at=NOW - timedelta(hours=1),
                tier_limit_24h=10,
                tier_usage_24h=10,
            ),
            _clock(),
        )

        assert decision.reason == "suppressed_block"

    def test_quota_outranks_staleness(self) -> None:
        decision = decide(
            _touch(
                quota=Allowance(proactive_touches_remaining=0),
                order_paid=True,
                proactive_sends_last_24h=9,
            ),
            _clock(),
        )

        assert decision.reason == "quota_exceeded"

    def test_staleness_outranks_the_rate_limit(self) -> None:
        decision = decide(
            _touch(order_paid=True, proactive_sends_last_24h=9),
            _clock(),
        )

        assert decision.reason == "stale_order_paid"

    def test_a_newer_message_outranks_a_paid_order(self) -> None:
        """Both are staleness; the contact talking is the fact the operator
        needs, because it is also what turns the funnel into a conversation."""
        decision = decide(
            _touch(last_inbound_at=EVENT_AT + timedelta(minutes=1), order_paid=True),
            _clock(),
        )

        assert decision.reason == "stale_newer_message"

    def test_the_contact_rate_limit_outranks_the_funnel_cooldown(self) -> None:
        decision = decide(
            _touch(proactive_sends_last_24h=1, other_funnel_touch_at=NOW - timedelta(hours=1)),
            _clock(),
        )

        assert decision.reason == "rate_limit_24h"

    def test_a_contact_protection_outranks_the_channel_pause(self) -> None:
        """The tier pause is transient and belongs to the number; the 72h
        cooldown belongs to this contact and outlives it."""
        decision = decide(
            _touch(
                other_funnel_touch_at=NOW - timedelta(hours=1),
                tier_limit_24h=10,
                tier_usage_24h=10,
            ),
            _clock(),
        )

        assert decision.reason == "funnel_cooldown_72h"


class TestGuards:
    """D2: the decision carries the facts it stood on, so S4's `WHERE` can
    revalidate them in the same short transaction as the insert."""

    def test_an_allowed_touch_carries_every_fact_that_supported_it(self) -> None:
        snapshot = _touch(
            inbound_seq=42,
            proactive_sends_last_24h=2,
            max_proactive_per_24h=4,
            other_funnel_touch_at=NOW - timedelta(days=9),
            tier_limit_24h=1000,
            tier_usage_24h=10,
        )

        guards = decide(snapshot, _clock()).guards

        assert guards.suppression_absent is True
        assert guards.order_unpaid is True
        assert guards.inbound_seq == 42
        assert guards.proactive_sends_24h == 2
        assert guards.max_proactive_per_24h == 4
        assert guards.other_funnel_touch_at == NOW - timedelta(days=9)
        assert guards.tier_limit_24h == 1000
        assert guards.tier_usage_24h == 10

    def test_the_effective_cap_in_the_guards_is_the_clamped_one(self) -> None:
        """What goes into the `WHERE` has to be what the rule actually used, or
        the revalidation would be laxer than the decision."""
        guards = decide(_touch(max_proactive_per_24h=9), _clock()).guards

        assert guards.max_proactive_per_24h == 4

    def test_a_denied_touch_still_carries_its_facts(self) -> None:
        """S4 cancels the touch with a reason; the facts are what an operator
        reads to understand why, months later."""
        guards = decide(
            _touch(suppression_reason="explicit_block", inbound_seq=42), _clock()
        ).guards

        assert guards.suppression_absent is False
        assert guards.inbound_seq == 42

    def test_a_paid_order_is_carried_as_a_fact_to_revalidate(self) -> None:
        guards = decide(_touch(order_paid=True), _clock()).guards

        assert guards.order_unpaid is False


class TestTheInjectedClock:
    """The 24h and 72h windows are read from the injected clock — no test here
    waits, and `test_no_direct_clock` fails the build if that ever slips.

    Staleness is no longer among them: dropping the 5-minute age gate took the
    clock out of that rung entirely, which is asserted in `TestStaleness`."""

    def test_the_funnel_cooldown_expires_by_moving_the_clock(self) -> None:
        snapshot = _touch(other_funnel_touch_at=NOW - timedelta(hours=71))
        clock = _clock()

        assert decide(snapshot, clock).reason == "funnel_cooldown_72h"

        clock.advance(timedelta(hours=1, seconds=1))

        assert decide(snapshot, clock).allow is True

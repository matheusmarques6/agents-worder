"""The protection ladder — may this proactive touch exist?

The third pure gate of the system, and the sibling of `agent_core.think_gate`
(should this input pay for extended reasoning?) and `judges.risk_gate` (does
this sent reply deserve an audit?). Same shape on purpose: a deterministic
function over a snapshot that was already loaded, returning the decision AND
its reason as DATA. Nothing here knows what a database is.

**Proactive only.** A reply to a message the contact just sent never comes
through this module — RF-034 is explicit that reactive messages are never
blocked by a rate limit, and the only anti-flood they get is the inbound
debounce. That is why the input type is named `ProactiveTouch` rather than
something neutral: the call site has to say out loud what it is asking about.

**The order is canonical and not negotiable:** supressão → quota → staleness →
limites (`CLAUDE.md`, "Runtime discipline"). It is not cosmetic. A suppressed
contact reported as "hit the rate limit" sends the operator to the wrong screen
and implies the touch goes out tomorrow, when in fact it never goes out again.

**One deliberate divergence from the letter of a requirement**, recorded here
because a rule that silently disagrees with its own doc is worse than either:
RF-032 asks for the staleness check on an event older than 5 minutes; the ladder
runs it always. The reasoning is in `STALE_AFTER`. Being stricter than the
requirement can only suppress a send, never create one.

**The guards are why this step exists on its own** (decision D2 of the E3
plan). Deciding and writing are different instants, so the ladder is a TOCTOU by
construction; the answer is not to hold a transaction open across the decision —
ADR-6 forbids exactly that — but to carry the facts forward. S4 writes the
outbox row through a compare-and-set whose `WHERE` revalidates each guard inside
the same short transaction as the insert. A fact that changed in between (a new
message, a payment, a block) fails the `WHERE`, and nothing is sent. The rule
lives here, in Python, once; SQL only re-asserts the facts it produced.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from agents_runtime import quota
from agents_runtime.clock import Clock
from agents_runtime.quota import Allowance

STALE_AFTER = timedelta(minutes=5)
"""RF-032 / arquitetura §5.3 ask for the staleness check on a LATE event — one
older than this. Documentation only: the ladder is **deliberately stricter** and
checks unconditionally. The age describes when the check became NECESSARY
(draining a queue after an outage), not permission to skip a protection while
the event is fresh — and since S3 the first touch of a funnel is born as a
`scheduled_touch` already due, so the fresh path IS the normal path. A contact
who wrote 30 seconds ago is in a live conversation the agent is already
answering by reaction; a funnel touch on top of that is exactly what annoys the
person we were trying to recover. Being stricter than the RF can only ever
suppress a send, never create one."""

PROACTIVE_WINDOW = timedelta(hours=24)
"""RF-034: the rolling window the per-contact touch count is measured over.

Counting is the caller's job, over `internal.message_outbox` — every proactive
origin, by `kind`, by `created_at`. The window is declared here and TRAVELS: the
snapshot loader and `internal.dispatch_touch` both receive it as a parameter
rather than writing `interval '24 hours'` of their own. That is deliberate and
it is the answer to a real hazard — a module that declares a window while
somebody else counts one is two numbers free to disagree in silence, and
`tests/db/test_dispatch_windows.py` is what keeps them the same number."""

FUNNEL_COOLDOWN = timedelta(hours=72)
"""RF-034: the gap a contact gets between two DIFFERENT funnels. Inside one
funnel the spacing is the funnel's own cadence — there is no per-funnel touch
cap.

Measured over `scheduled_touches.sent_at` of rows with `status = 'sent'` and a
different `funnel_id`, and it has to be measured there because funnel identity
exists in no other table — the outbox knows `kind`, not which funnel. Travels
as a parameter, for the same reason as `PROACTIVE_WINDOW`."""

DEFAULT_MAX_PER_CONTACT_24H = 1
PLATFORM_MAX_PER_CONTACT_24H = 4
"""The absolute ceiling of RF-034. D1 materialises it as a CHECK on
`tenants.proactive_max_per_contact_24h` in S2, and only the admin may raise a
tenant towards it. The clamp below is defence in depth, not the enforcement:
while the CHECK holds it is invisible, and it can only ever tighten."""

TIER_PAUSE_FRACTION = 0.8
"""RF-035: at 80% of the Meta tier for a number, proactive sends pause and the
admin is alerted. Reactive replies keep flowing — they never reach this
module."""

DENIAL_REASONS = (
    "suppressed_block",
    "suppressed_silence",
    "suppressed_optout",
    "quota_exceeded",
    "stale_newer_message",
    "stale_order_paid",
    "rate_limit_24h",
    "funnel_cooldown_72h",
    "channel_paused_tier",
)
"""Every reason `decide` can return other than `allowed`, in rung order.

Exported because it is a **contract with the schema**, not documentation: S2
materialises it as the CHECK on `scheduled_touches.cancel_reason` (plus
`manual`, the one cancellation a human causes and the ladder never produces),
and `tests/db/test_e3_schema.py` reads this tuple rather than retyping it. A
reason added here and not there is a cancellation that fails at 3am; a value
added there and not here is a metric bucket nothing can ever fill."""

#: `suppression_list.reason` (dicionário §4.4) → the ladder's vocabulary. An
#: operator's `manual` block is the same event to the contact as an explicit
#: block, so it lands on the same reason rather than inventing a fourth.
_SUPPRESSION_REASONS = {
    "explicit_block": "suppressed_block",
    "manual": "suppressed_block",
    "no_response_after_3": "suppressed_silence",
    "intent_optout": "suppressed_optout",
}

_SUPPRESSED_FALLBACK = "suppressed_block"
"""Fail closed. A row in `suppression_list` means somebody asked not to be
touched; a reason this code has not learned yet is not permission."""


@dataclass(frozen=True, slots=True)
class ProactiveTouch:
    """The snapshot the ladder weighs — already loaded, never fetched here.

    Every field is one fact read from the database in the short transaction that
    preceded the decision. Defaults describe a contact nothing is known against,
    so a caller (and a test) states exactly the facts that matter.
    """

    event_at: datetime
    """When the fact that justifies this touch happened — the abandonment, the
    unpaid PIX — not when the touch was scheduled. It is the instant "newer"
    is measured against, not an age: see `STALE_AFTER`."""

    inbound_seq: int = 0
    """`conversations.next_inbound_seq` as read. Not used by any rule: it is the
    guard, and it exists because a counter is what SQL can compare atomically —
    the same device as the central invariant's CAS in E1."""

    suppression_reason: str | None = None
    """The `suppression_list` row for this contact, if any."""

    quota: Allowance = field(default_factory=Allowance)
    """RF-073 / D9 — unlimited until plans exist."""

    last_inbound_at: datetime | None = None
    """The newest message from the contact in this conversation."""

    order_paid: bool = False
    """The order this funnel is chasing has been paid."""

    proactive_sends_last_24h: int = 0
    """Proactive touches this contact already received in `PROACTIVE_WINDOW`,
    from ALL origins — the limit protects the contact, not the funnel."""

    max_proactive_per_24h: int = DEFAULT_MAX_PER_CONTACT_24H
    """`tenants.proactive_max_per_contact_24h`, as configured. The effective
    value is this clamped to the platform ceiling."""

    other_funnel_touch_at: datetime | None = None
    """When a touch from a DIFFERENT funnel last went out to this contact."""

    tier_limit_24h: int | None = None
    """The Meta conversation tier of the number sending this. `None` for a
    channel with no tier (Evolution), where the ceilings are the sender's
    anti-ban ones instead (D10)."""

    tier_usage_24h: int = 0


@dataclass(frozen=True, slots=True)
class Guards:
    """The facts the decision stood on, shaped for the compare-and-set.

    Each field is one conjunct of the `WHERE` in `internal.dispatch_touch`
    (migration 20260806000004), so the write fails rather than sends when a fact
    moved between the decision and the insert. The clause each one became is
    written next to it, and the S4 lesson is why the *sources* are named as
    columns that exist: an earlier draft of this docstring invented a column
    (`is_proactive`) that no table has, which is a specification nobody can
    implement and a review nobody can fail.

    Two of the facts are carried as VALUES and compared (`inbound_seq`,
    `max_proactive_per_24h`); the rest are RECOMPUTED by the `WHERE` rather than
    compared, because for those a change in between can only ever make the
    predicate stricter — a suppression appears, an order is paid, a touch goes
    out — and the strict direction is the one a protection is allowed to move
    in. They are carried anyway, because what the decision saw is what an
    operator reads months later.

    `quota` is deliberately absent: there is no `quota_rules` table to
    revalidate against (D9), so there is no conjunct to write. It is the one
    fact of the ladder that the CAS cannot re-assert, and while the default is
    unlimited that costs nothing.
    """

    suppression_absent: bool
    """`AND NOT EXISTS (SELECT 1 FROM public.suppression_list s
    WHERE s.tenant_id = t.tenant_id AND s.contact_id = t.contact_id)` (RF-033).
    One row per contact — suppression is a state, not a log."""

    order_unpaid: bool
    """`AND NOT EXISTS (SELECT 1 FROM public.orders o
    WHERE o.id = t.order_id AND o.financial_status = 'paid')`, over the order the
    funnel is chasing (`scheduled_touches.order_id`). `paid` is a normalised
    value, not the platform's word — the CHECK on the column is what makes an
    unmapped vocabulary loud instead of silently never equal to `paid`."""

    inbound_seq: int
    """`AND EXISTS (SELECT 1 FROM public.conversations c
    WHERE c.id = t.conversation_id AND c.next_inbound_seq = $inbound_seq)` — a
    message that arrived in the meantime bumped the counter, the equality fails,
    and the touch dies with `cancel_reason = 'stale_newer_message'`: the
    ladder's own word, never a synonym (D7, and the S2 finding that two values
    for one fact break the S11 metric).

    The counter is the atomic device, the same one the central invariant's CAS
    uses in E1; the CAS also carries the same fact by its other name (`no
    inbound message newer than t.event_at`), because that is the shape the rule
    is written in and a guard should enforce the rule's own words."""

    proactive_sends_24h: int
    """What was counted, for the record. The enforcing conjunct recounts, over
    the outbox — the one door EVERY proactive send passes through, which is why
    RF-034's "all origins" cannot be counted on `scheduled_touches`:
    `AND (SELECT count(*) FROM internal.message_outbox o
    WHERE o.contact_id = t.contact_id AND o.kind IN ('funnel_touch','followup')
    AND o.created_at > now() - $proactive_window) < $max_proactive_per_24h`.
    By `created_at`, the commitment, not `sent_at`, the delivery: a touch
    already written and not yet delivered has to count, or a burst slips two
    touches past a limit of one."""

    max_proactive_per_24h: int
    """The EFFECTIVE cap — already clamped. What goes into the `WHERE` has to be
    what the rule used, or the revalidation would be laxer than the decision."""

    other_funnel_touch_at: datetime | None
    """`AND NOT EXISTS (SELECT 1 FROM public.scheduled_touches other
    WHERE other.contact_id = t.contact_id AND other.funnel_id <> t.funnel_id
    AND other.status = 'sent' AND other.sent_at > now() - $funnel_cooldown)` —
    recomputed rather than compared, because a touch appearing in between can
    only make the predicate stricter."""

    tier_limit_24h: int | None
    tier_usage_24h: int
    """`AND NOT EXISTS (SELECT 1 FROM public.conversations c
    JOIN public.channels_accounts ca ON ca.id = c.channel_account_id
    WHERE c.id = t.conversation_id AND ca.meta_tier IS NOT NULL
    AND ca.tier_usage_24h >= $tier_pause_fraction * ca.meta_tier)` — the number
    the touch would leave by. A NULL tier is Evolution, where the ceilings are
    the sender's anti-ban ones instead (D10)."""


@dataclass(frozen=True, slots=True)
class Decision:
    allow: bool
    reason: str
    guards: Guards


def decide(snapshot: ProactiveTouch, clock: Clock) -> Decision:
    """A loaded snapshot → whether this proactive touch may exist, why, and the
    facts that have to still be true when it is written.

    The first reason wins, and the order is the canonical one. Unlike the risk
    gate — which collects every reason, because an auditor wants everything that
    was at stake — a blocked touch has exactly one story to tell: the strongest
    reason it did not go out.
    """
    now = clock.now()
    effective_max = min(snapshot.max_proactive_per_24h, PLATFORM_MAX_PER_CONTACT_24H)
    guards = Guards(
        suppression_absent=snapshot.suppression_reason is None,
        order_unpaid=not snapshot.order_paid,
        inbound_seq=snapshot.inbound_seq,
        proactive_sends_24h=snapshot.proactive_sends_last_24h,
        max_proactive_per_24h=effective_max,
        other_funnel_touch_at=snapshot.other_funnel_touch_at,
        tier_limit_24h=snapshot.tier_limit_24h,
        tier_usage_24h=snapshot.tier_usage_24h,
    )

    def denied(reason: str) -> Decision:
        return Decision(allow=False, reason=reason, guards=guards)

    # 1. Suppression (RF-033). First, always: somebody asked not to be touched,
    #    and no other rung can produce a reason worth reporting over that one.
    if snapshot.suppression_reason is not None:
        return denied(_SUPPRESSION_REASONS.get(snapshot.suppression_reason, _SUPPRESSED_FALLBACK))

    # 2. Quota (RF-073, D9). Unlimited by default; called through the module so
    #    that the enforcement point is one named place when plans arrive.
    if not quota.has_headroom(snapshot.quota):
        return denied("quota_exceeded")

    # 3. Staleness (RF-032, arquitetura §5.3) — unconditionally, without the
    #    5-minute age gate the letter of the RF describes. See STALE_AFTER: the
    #    age says when the check became necessary, not when it may be skipped.
    #    The consequence is that this rung does not read the clock at all.
    #
    #    The contact talking outranks the payment: it is the fact that turns this
    #    funnel into a conversation (D7), and the one an operator needs.
    if snapshot.last_inbound_at is not None and snapshot.last_inbound_at > snapshot.event_at:
        return denied("stale_newer_message")
    if snapshot.order_paid:
        return denied("stale_order_paid")

    # 4. Limits (RF-034, RF-035). Contact protections first, channel last: the
    #    tier pause is transient and belongs to the number, while the per-contact
    #    windows belong to this person and outlive it.
    if snapshot.proactive_sends_last_24h >= effective_max:
        return denied("rate_limit_24h")

    if (
        snapshot.other_funnel_touch_at is not None
        and now - snapshot.other_funnel_touch_at < FUNNEL_COOLDOWN
    ):
        return denied("funnel_cooldown_72h")

    if (
        snapshot.tier_limit_24h is not None
        and snapshot.tier_usage_24h >= TIER_PAUSE_FRACTION * snapshot.tier_limit_24h
    ):
        return denied("channel_paused_tier")

    return Decision(allow=True, reason="allowed", guards=guards)

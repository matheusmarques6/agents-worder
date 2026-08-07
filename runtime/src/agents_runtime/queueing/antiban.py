"""Anti-ban — the rhythm a number is allowed to speak at (D10).

The fourth pure gate of the system, sibling of `dispatch.ladder`,
`agent_core.think_gate` and `judges.risk_gate`: a deterministic function over
facts that were already loaded, returning the decision AND its reason as DATA.
Nothing here knows what a database is, and nothing here composes content.

**Why it lives with the sender and not with the dispatch.** D10 splits the two
halves of "anti-ban" by what they are, not by convenience: the jitter, the
warm-up and the daily cap are *delivery rhythm* for one phone number, and the
sender is the only thing that talks to a provider (ADR-8). Copy variation is
*content*, has to be decided before the outbox row exists, and lives in
`dispatch/variation.py`. Mixing them would make the sender generate text — and a
sender that generates text needs a model, a tenant and a judge.

**Two asymmetries, both product decisions rather than engineering ones:**

  * **A reactive reply never waits and never hits a ceiling.** `CLAUDE.md` is
    explicit that reactive messages are never rate-limited — the only anti-flood
    they get is the inbound debounce. A 30-120s jitter on an answer to a
    customer is a customer waiting two minutes to be greeted, which is the
    failure that kills the support half of the product.
  * **The Cloud channel does not pass through here at all.** There the ceiling
    is the Meta tier and the ladder applies it before the outbox
    (`channel_paused_tier`). Two different risks: a banned CHIP on Evolution, a
    throttled ACCOUNT on Cloud.

Every number below is the canonical one from `CLAUDE.md` and travels from here;
none is written a second time in SQL.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from agents_runtime.clock import Clock
from agents_runtime.randomness import Randomness

JITTER_MIN = timedelta(seconds=30)
JITTER_MAX = timedelta(seconds=120)
"""`CLAUDE.md`, Evolution anti-ban: 30-120s between sends on one number. A
number that fires on a metronome reports itself."""

WARMUP_LADDER = (20, 50, 100)
"""Stage 0, 1, 2. A stage past the end of this ladder is a finished warm-up and
the hard cap takes over. A fresh chip that sends 300 on day one is a chip that
is banned on day one."""

DEFAULT_DAILY_CAP = 300
"""`channels_accounts.daily_cap` default. Held here as well because a `Pacing`
built by a caller that forgot the column must not default to unlimited."""

#: `channels_accounts.type`. This module speaks only about the unofficial one.
EVOLUTION = "evolution"

REFUSAL_REASONS = (
    "risk_not_accepted",
    "daily_cap",
    "warmup_cap",
    "jitter_wait",
)
"""Every reason `allowance` can return other than `allowed`, in rung order.

Exported for the same reason `ladder.DENIAL_REASONS` is: what stops a send gets
read by an operator months later, and each of these four asks for a different
action — accept the risk, wait for tomorrow, wait for the warm-up, wait a
minute. Collapsing them into "blocked" would collapse four investigations into
one dead end."""


@dataclass(frozen=True, slots=True)
class Pacing:
    """The facts about one number, read in the same claim as the row it paces.

    Loaded by `claim_outbox_batch` alongside the send, never by a second query:
    between a second query and the send, the world moves.
    """

    channel_type: str
    proactive: bool
    """`message_outbox.kind` in ('funnel_touch', 'followup'). The whole
    asymmetry of this module hangs on this one boolean, which is why it is
    resolved in SQL — from the column the outbox already carries — rather than
    inferred here from something that looks like it."""

    risk_accepted: bool = False
    """`channels_accounts.risk_accepted_at is not null`. Fail closed: a number
    whose acceptance we cannot see has not been accepted."""

    warmup_stage: int = 0
    daily_cap: int = DEFAULT_DAILY_CAP
    sends_today: int = 0
    """Proactive sends this number already made today. Counted in SQL against
    the day boundary, so a process that runs past midnight does not carry
    yesterday's count into today."""

    next_send_at: datetime | None = None
    """When this number's jitter expires. Written after each proactive send."""


@dataclass(frozen=True, slots=True)
class Verdict:
    allow: bool
    reason: str
    wait: timedelta | None = None
    """On `allow`, the jitter to write for the NEXT send. On `jitter_wait`, how
    long is left — so the sender puts the row back for exactly that long instead
    of guessing, and the message is not delayed longer than the rule asks."""


def daily_allowance(pacing: Pacing) -> int:
    """How many proactive sends this number may make today.

    The warm-up ceiling and the account's hard cap, whichever is tighter. Only
    tighter: the warm-up is the platform's protection and the merchant may lower
    the cap, never raise past it — the same single direction the proactive
    ceiling of D1 moves in.
    """
    stage = max(pacing.warmup_stage, 0)
    warmup = WARMUP_LADDER[stage] if stage < len(WARMUP_LADDER) else pacing.daily_cap
    return min(warmup, pacing.daily_cap)


def allowance(pacing: Pacing, clock: Clock, randomness: Randomness) -> Verdict:
    """May this row leave now, and if so, when may the next one.

    The first reason wins, and the order is deliberate — a number with no risk
    acceptance reported as "waiting for the jitter" would have an operator
    waiting for something that never resolves itself.
    """
    if pacing.channel_type != EVOLUTION or not pacing.proactive:
        # The two exits, and both are load-bearing enough to be the first thing
        # the function does: whatever else is true of this number, a reactive
        # reply and a Cloud row leave now.
        return Verdict(allow=True, reason="allowed")

    if not pacing.risk_accepted:
        return Verdict(allow=False, reason="risk_not_accepted")

    if pacing.sends_today >= daily_allowance(pacing):
        # Which ceiling was the binding one, by name. "Still warming up" and
        # "done for today" are different waits and different fixes.
        stage = max(pacing.warmup_stage, 0)
        warming = stage < len(WARMUP_LADDER) and WARMUP_LADDER[stage] <= pacing.daily_cap
        return Verdict(allow=False, reason="warmup_cap" if warming else "daily_cap")

    if pacing.next_send_at is not None:
        remaining = pacing.next_send_at - clock.now()
        if remaining > timedelta(0):
            return Verdict(allow=False, reason="jitter_wait", wait=remaining)

    seconds = randomness.uniform(JITTER_MIN.total_seconds(), JITTER_MAX.total_seconds())
    return Verdict(allow=True, reason="allowed", wait=timedelta(seconds=seconds))

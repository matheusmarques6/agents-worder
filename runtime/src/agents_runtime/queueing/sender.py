"""One sender pass: claim a batch, pace each, deliver each, record each outcome.

The pass opens with the unknown sweeps and then claims. The channel port may
raise; the classification rules of unidade 4 decide
between requeue-with-backoff and giving up. The delay is computed HERE, with
the injected randomness — the SQL applies it but never recalculates the
ladder, because a second copy of the canonical numbers is a divergence
waiting to happen.

`unknown` — the process dying between the provider accepting and us recording
it — is deliberately not handled here. That transition needs the reconciler
of cenários C, where it has tests; a hand-rolled version now would be the
blind resend ADR-8 forbids.

**S7 — the rhythm (D10).** Delivery rhythm is the sender's: the jitter, the
warm-up and the hard daily cap of an Evolution number are applied here, because
this is the only thing that talks to a provider (ADR-8). The rule itself is
`queueing/antiban.py`, pure; this file is where it meets a claimed row.

Two things about that meeting are load-bearing:

  * **a paced-out row is DEFERRED, not failed.** A number waiting out its jitter
    did not error — nothing was tried. `mark_outbox_failed` would write a
    `last_error` nobody caused and burn the attempt the claim just took, so a
    number in jitter would exhaust its own retries waiting its turn;
  * **the counter is bumped AFTER the provider accepts.** Before would be
    reserving a slot that might not be used; after counts what actually left,
    which is what both the daily cap and the Meta tier measure.

Content never enters here. The copy was decided before the outbox row existed
(`dispatch/variation.py`), because `message_outbox.payload` IS the final content
— a sender that composed text would need a model, a tenant and a judge.
"""

import uuid

import psycopg

from agents_runtime.channels.port import ChannelPort
from agents_runtime.clock import Clock
from agents_runtime.config import QueueingConfig
from agents_runtime.dispatch.ladder import TIER_PAUSE_FRACTION
from agents_runtime.queueing import antiban
from agents_runtime.queueing.backoff import delay_for
from agents_runtime.queueing.failures import Failure, classify
from agents_runtime.randomness import Randomness
from agents_runtime.repository import engine


async def sender_pass(
    conn: psycopg.AsyncConnection,
    channel: ChannelPort,
    *,
    config: QueueingConfig,
    randomness: Randomness,
    clock: Clock,
    limit: int = 50,
) -> int:
    """Returns how many sends were attempted — the pass's only observable.

    A row held back by the anti-ban rhythm is NOT an attempt, and does not count
    here: the number is what tells an operator whether the sender is working, and
    counting a deferral would make a fully paused number look busy.
    """
    # House-keeping before claiming: a dead sender's 'sending' rows become
    # unknown (state only, NEVER a resend), and unknowns past the review
    # window go to a human. Running here covers the startup case for free —
    # the first pass of a fresh process is the sweep-at-boot.
    await engine.sweep_outbox_unknown(conn)
    await engine.review_stale_unknown(conn, review_after=config.unknown_review_after)

    token = uuid.uuid4()
    batch = await engine.claim_outbox_batch(
        conn, token, lease=config.send_lease, limit=limit
    )

    attempted = 0
    for row in batch:
        send = row.send

        verdict = antiban.allowance(row.pacing, clock, randomness)
        if not verdict.allow:
            # Back to `pending`, for exactly as long as the rule asks. The jitter
            # knows what is left of itself; a ceiling has to wait for the day to
            # turn, and `config.paced_retry` is the sweep that rechecks it.
            await engine.defer_outbox_send(
                conn,
                send.outbox_id,
                token,
                retry_in=verdict.wait if verdict.wait is not None else config.paced_retry,
            )
            continue

        attempted += 1
        try:
            provider_message_id = await channel.send(send)
        except Exception as error:  # the classifier is the policy
            failure = classify(error)
            await engine.mark_outbox_failed(
                conn,
                send.outbox_id,
                token,
                transient=failure is not Failure.PERMANENT,
                error=str(error)[:500],
                retry_in=delay_for(
                    send.attempt_count, config=config, randomness=randomness
                ),
            )
        else:
            await engine.mark_outbox_sent(conn, send.outbox_id, token, provider_message_id)
            # What left is what counts — for the daily cap, for the number's next
            # jitter and for the Meta tier (RF-035). Reactive rows are handed
            # over too, and the SQL is what refuses to count them: the asymmetry
            # is stated once, where the counters are.
            await engine.record_channel_send(
                conn,
                row.channel_account_id,
                proactive=row.pacing.proactive,
                next_send_at=(clock.now() + verdict.wait) if verdict.wait else None,
                tier_pause_fraction=TIER_PAUSE_FRACTION,
            )

    return attempted

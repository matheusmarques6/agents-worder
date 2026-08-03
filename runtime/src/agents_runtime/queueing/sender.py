"""One sender pass: claim a batch, deliver each, record each outcome.

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
"""

import uuid

import psycopg

from agents_runtime.channels.port import ChannelPort
from agents_runtime.config import QueueingConfig
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
    limit: int = 50,
) -> int:
    """Returns how many sends were attempted — the pass's only observable."""
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

    for send in batch:
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

    return len(batch)

"""One sender pass: claim a batch, deliver each, record each outcome.

The channel port may raise; the classification rules of unidade 4 decide
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
    token = uuid.uuid4()
    batch = await engine.claim_outbox_batch(conn, token, limit=limit)

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

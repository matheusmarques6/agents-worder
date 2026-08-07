"""The sweep that turns the milestone's silent failures into rows somebody reads.

E3 shipped with exactly one alert: the crossing of 80% of the Meta tier
(RF-035), written by `internal.record_channel_send` in S7. Three other ways for
this milestone to stop talking had no voice at all, and each of them is a
message that never reaches a customer while every dashboard stays green:

  * **a touch stuck in `enqueued`** — the S4 finding. The claim marks the touch
    and enqueues the job in one transaction; if the job dies in the DLQ, the
    touch is left in a state `claim_due_touches` never looks at again (it only
    takes `pending`);
  * **a banned number** — nothing on the send path reads
    `channels_accounts.status`, so a dead number keeps collecting outbox rows;
  * **a store failing to reconcile for hours** — S8 closes a failed pass in
    `sync_status = 'error'` and nobody looks.

**Age, never a second clock.** The stuck touch gets an alert and no repair, and
that is the decision recorded in the plan rather than a shortcut: a sweep that
returned the touch to `pending` could resend a message that already went out,
which is precisely the duplicate the S4 compare-and-set exists to prevent. The
one who fixes a stuck touch is a person, and this sweep is how the person hears
about it.

The rule about the queue's own depth and DLQ age is deliberately NOT here: those
are metrics of the queue, they belong to the OTLP exporter, and the exporter
depends on credentials this repository does not have (pendências B-2/B-3).
"""

from datetime import timedelta

import psycopg

from agents_runtime.repository import alerts as alerts_repo


async def health_pass(
    conn: psycopg.AsyncConnection,
    *,
    touch_stuck_after: timedelta,
    sync_error_after: timedelta,
) -> int:
    """One sweep. Returns how many alerts it opened — new situations, not total.

    An already-open alert of the same kind suppresses a second one, so a healthy
    process reports zero every tick and a number here always means something
    changed. That is what keeps the merchant's alert list readable on the day the
    DLQ fills up.
    """
    async with conn.transaction():
        return await alerts_repo.sweep_health_alerts(
            conn,
            touch_stuck_after=touch_stuck_after,
            sync_error_after=sync_error_after,
        )

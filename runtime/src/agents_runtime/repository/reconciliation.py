"""The three statements the reconciliation is allowed to make.

There is no INSERT here, and that absence is the design. The poll does not
write `webhook_events` — it calls `internal.ingest_webhook`, the same function
the Edge Function calls (D5). A second write path would be a second
idempotency to keep in step with the first, and the day they drifted the only
symptom would be a customer receiving the same message twice.

So what is left is a claim, a close, and a call at somebody else's door.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from agents_runtime.connectors.port import PlatformEvent, SyncTarget
from agents_runtime.obs.context import Carrier


async def claim_sync_targets(
    conn: psycopg.AsyncConnection, *, stale_after: timedelta, limit: int
) -> Sequence[SyncTarget]:
    """The stores nobody has asked in a while, marked `syncing` and handed over.

    `stale_after` travels as a parameter for the reason every canonical number
    in this codebase does: written as an interval literal in the SQL it would
    be a second copy of `QueueingConfig.reconcile_stale_after`, free to
    disagree with the first the day somebody changed one of them.
    """
    cursor = await conn.execute(
        "select * from internal.claim_sync_targets(%s, %s)", (stale_after, limit)
    )
    return [
        SyncTarget(
            connector_account_id=row[0],
            tenant_id=row[1],
            platform=row[2],
            source_account_id=row[3],
            cursor=row[4],
        )
        for row in await cursor.fetchall()
    ]


async def finish_sync(
    conn: psycopg.AsyncConnection,
    connector_account_id: UUID,
    *,
    status: str,
    cursor_at: datetime | None,
) -> datetime | None:
    """Close a pass. Returns the cursor as it now stands — which may not be ours.

    The database refuses a cursor that would go backwards, and returns what it
    kept instead of raising: a rejected cursor is not an error, it is a slow
    page arriving after a fast one, and the caller's business is to see the
    truth rather than to be interrupted by it.
    """
    cursor = await conn.execute(
        "select internal.finish_sync(%s, %s, %s)",
        (connector_account_id, status, cursor_at),
    )
    return (await cursor.fetchone())[0]


async def ingest_polled_event(
    conn: psycopg.AsyncConnection,
    target: SyncTarget,
    event: PlatformEvent,
    *,
    otel: Carrier | None = None,
) -> str:
    """Hand a polled fact to the ingestion — the SAME door the webhook uses.

    `ingested` · `duplicate` · `unknown_account`, exactly the vocabulary the
    Edge Function gets. `duplicate` is the ordinary case and not a problem: it
    means the webhook already delivered this, which is what a safety belt
    mostly discovers.

    Nothing about the caller travels in the arguments — with one deliberate
    exception. The tenant is resolved inside, from the store, because a
    `tenant_id` chosen by the poll would be the trust boundary broken from the
    inside, where nobody is looking. `otel` is different in kind: it is not a
    fact about the data, it is the W3C context of the sweep that knocked, and it
    goes into the `q_domain_events` job so that "store X was reconciled" and "Y's
    funnel was cancelled because the order was paid" are one trace instead of
    two.

    `p_otel =>` is named notation, and it is not style: `p_debounce` sits between
    the two in the signature, and reaching past it positionally would mean
    writing the canonical 10s debounce here — a second copy of a number that
    lives in `QueueingConfig`, free to disagree with the first. Named notation
    lets the database's own default answer for it.
    """
    cursor = await conn.execute(
        "select status from internal.ingest_webhook(%s, %s, %s, %s, %s, p_otel => %s)",
        (
            target.platform,
            target.source_account_id,
            event.external_event_id,
            event.event_type,
            Jsonb(dict(event.payload)),
            Jsonb(dict(otel)) if otel else None,
        ),
    )
    return (await cursor.fetchone())[0]

"""The reconciliation sweep — the safety belt of ADR-3.

A webhook is a third party's promise. The platform falls over, our Edge
Function answers 500 during a deploy, Meta redelivers and Shopify does not —
and the paid order nobody saw becomes a funnel chasing a customer who already
paid. This pass exists so that losing ONE delivery is a fifteen-minute delay
instead of a permanent error.

Everything it does is a consequence of D5, which says the poll calls the same
`internal.ingest_webhook` the webhook calls:

  * it never writes an event. It translates (in the adapter), then knocks;
  * `duplicate` is the ordinary answer, not a failure. Most of what a safety
    belt finds is something that already arrived;
  * an event only the poll saw enters through exactly the same door, gets the
    same row, the same `q_domain_events` job and the same router — so "the poll
    saw it" and "the webhook brought it" are indistinguishable downstream, and
    there is no second behaviour to keep in step.

**The cursor names an ingested event, never a requested window.** One pass
takes one page; the close writes the instant of the last event the ingestion
ACCEPTED, and a pass that never got that far writes nothing at all. The next
pass resumes from there, inclusively, so the boundary event is re-read and
refused as a duplicate. Moving the cursor to the end of the window we asked for
is the one bug this file exists not to have — an event skipped because we
requested it and never received it. `internal.finish_sync` is what makes "never
regresses" a property of the column instead of a discipline of the callers.

One store's failure is one store's failure. A platform that is down must not
stop the reconciliation of the other tenants — so the sweep isolates per
account and records the outcome in `sync_status`, which until this step was a
column with no writer at all.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import psycopg

from agents_runtime.connectors.port import ConnectorPort
from agents_runtime.obs.context import TraceSource, no_trace_context
from agents_runtime.repository import reconciliation as repo

#: How many events one store hands over in one pass. A bound, not a limit on
#: the sync: what is left is picked up by the next tick, from a cursor that only
#: moved as far as an ingested event. DECISION, not a canonical number — it
#: trades the size of a pass against how long one store may hold the sweep.
PAGE_SIZE = 200

#: How many stores one pass claims. Same reading, one level up.
STORES_PER_PASS = 20


@dataclass(frozen=True)
class ReconcileResult:
    """What one sweep did — the numbers a test asserts and S11 will count."""

    stores: int = 0
    ingested: int = 0
    duplicates: int = 0
    #: `(source_account_id, reason)` per store that did not finish. Carried out
    #: rather than swallowed: `sync_status = 'error'` says WHICH store broke and
    #: this says how, and a sweep that could only do the first would be a sweep
    #: whose diagnosis lives in a log we do not have yet (S10).
    failures: tuple[tuple[str, str], ...] = field(default_factory=tuple)


async def reconcile_pass(
    conn: psycopg.AsyncConnection,
    connectors: Mapping[str, ConnectorPort],
    *,
    stale_after: timedelta,
    stores: int = STORES_PER_PASS,
    page_size: int = PAGE_SIZE,
    trace: TraceSource = no_trace_context,
) -> ReconcileResult:
    """One sweep. Claims the stale stores, polls each, closes each.

    `trace` is the W3C context this pass carries into every `q_domain_events` job
    the ingestion creates on its behalf. It matters more here than on the webhook
    path, and D5 is the reason: the poll and the webhook enter by the same door,
    but the webhook's trace starts outside our process while this one starts at a
    tick of ours. Without it, "store X was reconciled" and "Y's funnel died
    because the order was paid" are two unrelated traces telling one story.

    Read ONCE per pass, like the dispatcher's: every job of one sweep belongs to
    that sweep. The default says the honest thing — nothing is instrumented yet,
    because the SDK and the exporter depend on Logfire and Grafana Cloud
    (pendências B-2/B-3).
    """
    context = trace()
    targets = await repo.claim_sync_targets(conn, stale_after=stale_after, limit=stores)

    ingested = 0
    duplicates = 0
    failures: list[tuple[str, str]] = []

    for target in targets:
        connector = connectors.get(target.platform)
        reached: datetime | None = None
        status = "ok"

        try:
            if connector is None:
                # A store on a platform this deployment has no adapter for.
                # `error` and not `ok`: `ok` would claim we looked. The
                # merchant's store is connected and unreconciled, which is a
                # fact the hub has to be able to show.
                raise LookupError(f"sem adaptador para {target.platform}")

            for event in await connector.fetch_since(target, limit=page_size):
                outcome = await repo.ingest_polled_event(conn, target, event, otel=context)
                if outcome == "ingested":
                    ingested += 1
                elif outcome == "duplicate":
                    duplicates += 1
                else:
                    # `unknown_account`: the store stopped being resolvable
                    # between the claim and now — disconnected in the hub,
                    # reconnected under another id. Stop: the cursor keeps what
                    # was ingested and nothing after it is invented.
                    raise RuntimeError(f"a ingestão recusou a loja: {outcome}")
                # After the ingestion accepted it, never before. A page is
                # ingested in order and the cursor follows it, so the close
                # below can only ever name an event that is already a row.
                reached = event.occurred_at
        except Exception as failure:  # one store, never the sweep
            # Broad on purpose and narrow in effect: the sweep is cross-tenant,
            # so an adapter raising anything at all must cost exactly one store.
            # The cost of the breadth is that a bug of ours lands as
            # `sync_status = 'error'` too — visible, which is the trade.
            status = "error"
            failures.append((target.source_account_id, _reason(failure)))

        try:
            await repo.finish_sync(
                conn, target.connector_account_id, status=status, cursor_at=reached
            )
        except Exception as failure:  # see above, and worse
            # The close is inside the isolation too, and this is not paranoia:
            # `finish_sync` raises when the store no longer exists, which is
            # exactly what a merchant disconnecting mid-sweep produces. Outside
            # the guard it would abort the pass and leave every store CLAIMED
            # BUT NOT CLOSED after it — stuck in `syncing`, one tenant's
            # disconnection freezing everybody else's reconciliation, with no
            # error anywhere because `syncing` looks like work in progress.
            failures.append((target.source_account_id, _reason(failure)))

    return ReconcileResult(
        stores=len(targets),
        ingested=ingested,
        duplicates=duplicates,
        failures=tuple(failures),
    )


def _reason(failure: BaseException) -> str:
    """The diagnosis that travels out, until S10 gives it a log to live in."""
    return f"{type(failure).__name__}: {failure}"

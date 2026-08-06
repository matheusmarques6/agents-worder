"""The connector port — the one door IN from an e-commerce platform.

`channels` is the door out; this is the door in, and the two never know about
each other (import-linter says so). Everything the engine knows about Shopify,
Nuvemshop and Yampi fits in this file: a store to sync goes in, a sequence of
platform events comes out, already spoken in the vocabulary the ingestion
understands.

That last clause is the whole design. The reconciliation of S8 does NOT get a
write path of its own — it hands what it polled to the same
`internal.ingest_webhook` the webhook uses (D5), because a second write path
would be a second idempotency to keep in step with the first. So the adapter's
job is translation, not persistence: whatever Shopify calls `orders/paid`
arrives here as `order_paid`, with the same payload shape the Edge Function
would have built, or it does not arrive at all.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

#: The event vocabulary `internal.apply_domain_event` routes on. An event type
#: outside this set is not "unsupported", it is UNTRANSLATED: it would be
#: ingested, enqueued, and then discarded by the router with a trail nobody
#: reads — a poll that looks like it worked and changes nothing. The port
#: refuses it at construction so the failure lands on the adapter that forgot
#: to translate, not on a status column three tables away.
PLATFORM_EVENT_TYPES = frozenset(
    {"checkout_abandoned", "cart_abandoned", "pix_pending", "order_paid"}
)


@dataclass(frozen=True, slots=True)
class SyncTarget:
    """One store to reconcile — a row of `connector_accounts`, as the poll sees it.

    `cursor` is where the last successful poll stopped, and `None` means this
    store has never been polled. It is a timestamp rather than a page token on
    purpose: every platform here exposes an "updated since" filter, and a
    timestamp is the one cursor whose ordering is defined without asking the
    provider. Ties (two orders in the same second) are re-read on the next
    poll, which costs exactly nothing — re-reading is what D5 makes free.
    """

    connector_account_id: UUID
    tenant_id: UUID
    platform: str
    source_account_id: str
    cursor: datetime | None


@dataclass(frozen=True, slots=True)
class PlatformEvent:
    """One fact from the platform, already shaped like the webhook that carries it.

    Three rules, all checked here rather than trusted:

      * `external_event_id` is non-empty. It is half of the idempotency key
        (`source`, `source_account_id`, `external_event_id`), and an empty one
        would make every polled event of a store collide with every other —
        the replay proof would pass while the data was being eaten;
      * `event_type` is one the router knows (see `PLATFORM_EVENT_TYPES`);
      * `occurred_at` is timezone-aware. It becomes the account's cursor, and a
        naive value cannot be compared with the aware one already stored.
    """

    external_event_id: str
    event_type: str
    occurred_at: datetime
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.external_event_id:
            raise ValueError(
                "evento sem external_event_id — sem ele a idempotência da ingestão "
                "não distingue dois eventos da mesma loja"
            )
        if self.event_type not in PLATFORM_EVENT_TYPES:
            raise ValueError(
                f"event_type não traduzido: {self.event_type!r} — "
                f"o roteador conhece {sorted(PLATFORM_EVENT_TYPES)}. "
                "Traduzir é trabalho do adaptador, não do banco."
            )
        if self.occurred_at.tzinfo is None:
            raise ValueError(
                f"occurred_at ingênuo: {self.occurred_at!r} — o cursor da conta é "
                "timezone-aware e a comparação estouraria no primeiro poll"
            )


class ConnectorPort(Protocol):
    """Everything the platform changed since the target's cursor, oldest first.

    Contract, and the reconciliation depends on every clause:

      * **ascending `occurred_at`.** The cursor advances event by event, so an
        out-of-order batch would advance it past something not yet ingested;
      * **inclusive lower bound.** An event exactly AT the cursor is returned
        again. That is deliberate: it is what makes a tie safe, and re-reading
        costs one duplicate row in `webhook_events`;
      * **raise on failure.** The port never returns a short batch to mean
        "the platform is down" — a short batch is indistinguishable from "that
        is all there was", and the difference is whether the cursor may move.

    `limit` bounds one pass, never the sync: what is left is picked up by the
    next tick, from a cursor that only moved as far as it was allowed to.
    """

    async def fetch_since(self, target: SyncTarget, *, limit: int) -> Sequence[PlatformEvent]: ...

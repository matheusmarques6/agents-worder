"""The real Shopify connector — REST Admin API, transport injectable.

In the mould of `channels/cloud_api.py`: one HTTP client, a transport that can
be handed in, errors surfaced as `RuntimeError("HTTP {status} ...")` so the
sweep's failure reading is one vocabulary. What is different is where the
address comes from, and it is different for a reason worth stating:

**Shopify's host is per store.** The Meta adapter talks to one graph for every
tenant; here the host IS `connector_accounts.source_account_id`
(`loja.myshopify.com`), and the token is per store too. So this file names no
provider address at all — it composes one out of the row the claim handed it,
and holds nothing but a path. The fitness function that lets only two adapters
name a provider host is not being evaded: there is genuinely nothing to name.

**The event id is derived from the FACT, never from the delivery.** Shopify's
`X-Shopify-Webhook-Id` is unique per delivery attempt, so an event keyed on it
would be a different event every time — the poll and the webhook would never
collide and D5's "one effect" would quietly become "one per path". The id here
is `{event_type}:{the platform's id for the thing}`, and **the Shopify Edge
Function, when it is written, must derive it identically**. That is a
dependency this file creates and cannot enforce; it is named here because no
Shopify ingestion function exists yet to name it in.

Two endpoints, two occasions:

  * `orders.json` filtered to paid → `order_paid`;
  * `checkouts.json` (Shopify's name for abandoned checkouts) →
    `checkout_abandoned`.

`cart_abandoned` and `pix_pending` are deliberately absent: Shopify has no cart
abandonment webhook and no PIX. Emitting them from something that resembles
them would be inventing a merchant's fact.
"""

import os
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import httpx

from agents_runtime.connectors.port import ConnectorPort, PlatformEvent, SyncTarget
from agents_runtime.connectors.secrets import (
    ConnectorSecrets,
    VaultConnectorSecrets,
    single_token_from_env,
)

#: A released, stable version — not the newest, the same reading as the Cloud
#: API adapter's v19.0. Overridable per environment because Shopify retires a
#: version roughly a year after its release, and the `contract` suite is the
#: only thing that can notice before production does.
DEFAULT_API_VERSION = "2026-01"

SCHEME = "https"

#: Shopify's own word for a paid order. Anything else is not a payment, and
#: emitting `order_paid` for it would disarm a funnel chasing money that never
#: arrived.
PAID = "paid"


class ShopifyConnector:
    """One door in, per platform. Only the reconciliation holds an instance."""

    def __init__(
        self,
        secrets: ConnectorSecrets,
        *,
        api_version: str = DEFAULT_API_VERSION,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._secrets = secrets
        self._api_version = api_version
        # No `base_url`: the host is per store, so it is composed per request
        # from the target. One client, many shops — which is also what keeps the
        # connection pool one pool instead of one per merchant.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0), transport=transport
        )

    async def fetch_since(
        self, target: SyncTarget, *, limit: int
    ) -> Sequence[PlatformEvent]:
        token = await self._secrets.token_for(target)
        since = target.cursor

        events = [
            *self._paid_orders(await self._get(target, token, "orders", since, limit)),
            *self._abandoned(await self._get(target, token, "checkouts", since, limit)),
        ]
        # Ascending, always: the port's contract is that the cursor may advance
        # event by event, and two endpoints merged in fetch order would not be
        # ordered at all.
        return sorted(events, key=lambda event: event.occurred_at)

    # -- transport ----------------------------------------------------------

    async def _get(
        self,
        target: SyncTarget,
        token: str,
        resource: str,
        since: datetime | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if resource == "orders":
            # Shopify defaults to open orders only; a paid order that has been
            # fulfilled and closed is exactly the one whose funnel must stop.
            params["status"] = "any"
            params["financial_status"] = PAID
        if since is not None:
            # Inclusive on Shopify's side too, which is what the port's
            # inclusive lower bound is built on: a tie is re-read, never lost.
            params["updated_at_min"] = since.isoformat()

        response = await self._client.get(
            httpx.URL(
                scheme=SCHEME,
                host=target.source_account_id,
                path=f"/admin/api/{self._api_version}/{resource}.json",
            ),
            params=params,
            headers={"X-Shopify-Access-Token": token},
        )
        if response.status_code >= 400:
            # The sweep reads the status out of this message, exactly as the
            # sender's classifier does: 429/5xx are the platform, 4xx are us.
            raise RuntimeError(f"HTTP {response.status_code} {response.text[:300]}")
        return list(response.json().get(resource) or [])

    # -- translation --------------------------------------------------------

    def _paid_orders(self, orders: list[dict[str, Any]]) -> list[PlatformEvent]:
        events = []
        for order in orders:
            if order.get("financial_status") != PAID:
                # The server-side filter above already asks for this, and this
                # is the brace: `order_paid` CANCELS a funnel, so a provider
                # that ignored the parameter (or an endpoint whose default
                # changes) must not be able to disarm the recovery of money
                # that never arrived. The cost of the second check is one
                # comparison; the cost of trusting the first is a lost sale we
                # would never hear about.
                continue
            events.append(
                PlatformEvent(
                    external_event_id=f"order_paid:{order.get('id')}",
                    event_type="order_paid",
                    occurred_at=_instant(order.get("updated_at")),
                    payload={
                        "phone": _phone(order),
                        "order": _order(order, financial_status=PAID),
                    },
                )
            )
        return events

    def _abandoned(self, checkouts: list[dict[str, Any]]) -> list[PlatformEvent]:
        events = []
        for checkout in checkouts:
            # The token, not the id: it is what `abandoned_checkout_url` carries
            # and therefore what a webhook body identifies the same checkout by.
            identity = checkout.get("token") or checkout.get("id")
            events.append(
                PlatformEvent(
                    external_event_id=f"checkout_abandoned:{identity}",
                    event_type="checkout_abandoned",
                    occurred_at=_instant(checkout.get("updated_at")),
                    payload={
                        "phone": _phone(checkout),
                        "order": _order(checkout, financial_status=None),
                    },
                )
            )
        return events

    async def aclose(self) -> None:
        await self._client.aclose()


def _instant(raw: Any) -> datetime:
    """Shopify's ISO-8601 with offset, as an aware datetime.

    A value we cannot parse raises rather than defaulting to `now()`: the
    instant becomes the account's cursor, and a cursor invented from the clock
    would jump the sweep past everything older than the moment we failed.
    """
    if not isinstance(raw, str):
        raise ValueError(f"instante ausente na resposta da plataforma: {raw!r}")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError(f"instante sem fuso: {raw!r}")
    return parsed


def _phone(record: dict[str, Any]) -> str | None:
    """The best phone the record has, cleaned of punctuation and nothing more.

    Deliberately NOT normalised into E.164 by guessing a country code. Shopify
    stores whatever the customer typed, and a `(11) 99999-0000` that we decided
    was Brazilian would be a message sent to a number a human never confirmed —
    possibly a real person who is not this customer. Cleaned and passed through,
    a phone that is not E.164 is refused by `apply_domain_event` and leaves a
    `failed` webhook event a human can look at. Visible and wrong beats
    invisible and confident.
    """
    for candidate in (
        record.get("phone"),
        (record.get("customer") or {}).get("phone"),
        (record.get("shipping_address") or {}).get("phone"),
        (record.get("billing_address") or {}).get("phone"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            cleaned = "".join(ch for ch in candidate if ch.isdigit() or ch == "+")
            if cleaned:
                return cleaned
    return None


def _order(record: dict[str, Any], *, financial_status: str | None) -> dict[str, Any]:
    """The order sub-document `internal.mirror_order` reads.

    Its keys are the mirror's keys and not Shopify's, which is the translation
    the port exists to demand: `external_id`, `total`, `currency`, `items`,
    `customer`. The mirror takes each field only when it is well formed, so
    passing a value through unmapped would not be permissive — it would be
    silently dropped.
    """
    customer = record.get("customer") or {}
    order: dict[str, Any] = {
        "external_id": str(record.get("id", "")),
        "total": record.get("total_price"),
        "currency": record.get("currency"),
        "items": [
            {
                "sku": item.get("sku"),
                "title": item.get("title"),
                "qty": item.get("quantity"),
                "price": item.get("price"),
            }
            for item in record.get("line_items") or []
        ],
    }
    if financial_status is not None:
        order["financial_status"] = financial_status
    if customer.get("id"):
        order["customer"] = {
            "external_id": str(customer["id"]),
            "name": " ".join(
                part for part in (customer.get("first_name"), customer.get("last_name")) if part
            )
            or None,
            "email": customer.get("email"),
            "phone": _phone({"customer": customer}),
        }
    return order


def from_env(dsn: str | None = None) -> ConnectorPort:
    """The production factory — Vault, and the E0-22 hole it is honest about.

    Takes the DSN and ignores it, exactly as `channels/cloud_api.py:from_env`
    does: the entrypoint calls every seam factory the same way, and a factory
    with a different signature would be a special case in the one function that
    exists to not have any. When `get_connector_secret` (E0-22) lands, the DSN
    is what it will be reached through, and the parameter is already here.

    `AGENTS_SHOPIFY_SINGLE_TOKEN` switches to the one-store stand-in, and it is
    opt-in rather than a fallback on purpose: a factory that silently used an
    environment token when Vault was unavailable would be the trust boundary
    crossed by a default, in the one code path nobody re-reads.
    """
    secrets: ConnectorSecrets = (
        single_token_from_env()
        if os.environ.get("AGENTS_SHOPIFY_SINGLE_TOKEN")
        else VaultConnectorSecrets()
    )
    return ShopifyConnector(
        secrets,
        api_version=os.environ.get("AGENTS_SHOPIFY_API_VERSION", DEFAULT_API_VERSION),
    )

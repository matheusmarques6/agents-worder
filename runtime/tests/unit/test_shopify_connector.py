"""The adapter, against a transport that answers — never against Shopify.

Same shape as `test_cloud_api_channel.py`: `httpx.MockTransport` stands where
the network would be, so this is a `unit` test in the strict sense of the level
(no I/O at all) while exercising the real client, the real URL composition and
the real translation.

What is asserted here is the translation, because the translation is where this
adapter can be wrong in a way nothing downstream would catch. An event whose
`event_type` is untranslated dies at the port (S8 (a)). An event that is
translated INCORRECTLY — a pending order announced as paid, a timestamp
invented from the clock — passes every guard on the way in and does damage on
the way out.
"""

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from agents_runtime.connectors.port import SyncTarget
from agents_runtime.connectors.secrets import (
    ConnectorSecretUnavailable,
    SingleTokenSecrets,
    VaultConnectorSecrets,
)
from agents_runtime.connectors.shopify import DEFAULT_API_VERSION, ShopifyConnector

pytestmark = pytest.mark.unit

SHOP = "loja-do-bruno.myshopify.com"
TOKEN = "shpat_faketoken"

TARGET = SyncTarget(
    connector_account_id=uuid4(),
    tenant_id=uuid4(),
    platform="shopify",
    source_account_id=SHOP,
    cursor=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
)


def _connector(orders=(), checkouts=(), *, record: list | None = None) -> ShopifyConnector:
    def handle(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        body = {"orders": list(orders)} if "orders" in request.url.path else {
            "checkouts": list(checkouts)
        }
        return httpx.Response(200, json=body)

    return ShopifyConnector(
        SingleTokenSecrets(source_account_id=SHOP, token=TOKEN),
        transport=httpx.MockTransport(handle),
    )


def _order(**overrides) -> dict:
    return {
        "id": 450789469,
        "updated_at": "2026-08-06T13:00:00-03:00",
        "financial_status": "paid",
        "total_price": "199.90",
        "currency": "BRL",
        "phone": "+5511999990000",
        "line_items": [{"sku": "A1", "title": "Camiseta", "quantity": 1, "price": "199.90"}],
        **overrides,
    }


class TestAPendingOrderIsNotAPayment:
    async def test_a_paid_order_becomes_order_paid(self) -> None:
        (event,) = await _connector(orders=[_order()]).fetch_since(TARGET, limit=250)

        assert event.event_type == "order_paid"
        assert event.payload["order"]["external_id"] == "450789469"
        assert event.payload["order"]["total"] == "199.90"

    async def test_an_unpaid_order_produces_nothing(self) -> None:
        # `order_paid` CANCELS a funnel (S5). Announcing one for an order that
        # is `pending`, `authorized` or `refunded` would disarm the recovery of
        # money that never arrived — the exact failure the milestone is built
        # to avoid, arriving through the door built to prevent it.
        for status in ("pending", "authorized", "partially_paid", "refunded", "voided"):
            events = await _connector(
                orders=[_order(financial_status=status)]
            ).fetch_since(TARGET, limit=250)
            assert events == [], f"{status} virou pagamento"

    async def test_the_query_also_asks_the_platform_to_filter(self) -> None:
        # Belt and braces, and the braces are the ones above: the server-side
        # filter saves a page of bandwidth, and a provider that ignored the
        # parameter would still not produce a false payment.
        seen: list[httpx.Request] = []
        await _connector(orders=[], record=seen).fetch_since(TARGET, limit=250)

        orders = next(r for r in seen if "orders" in r.url.path)
        assert orders.url.params["financial_status"] == "paid"
        assert orders.url.params["status"] == "any"


class TestTheInstantIsNeverInvented:
    async def test_an_unparseable_updated_at_raises(self) -> None:
        # It becomes the account's cursor. Defaulting to the clock would move
        # the cursor to NOW on a malformed page, and everything older than this
        # moment — including whatever the webhook lost — would never be asked
        # for again. A raise costs one store one pass; a default costs history.
        with pytest.raises(ValueError, match="instante"):
            await _connector(orders=[_order(updated_at=None)]).fetch_since(
                TARGET, limit=250
            )

    async def test_the_offset_is_kept_not_dropped(self) -> None:
        (event,) = await _connector(orders=[_order()]).fetch_since(TARGET, limit=250)

        assert event.occurred_at == datetime(2026, 8, 6, 16, 0, tzinfo=UTC)


class TestTheIdIsTheFactNotTheDelivery:
    async def test_two_polls_of_the_same_order_produce_the_same_id(self) -> None:
        # This is what makes D5 true across the two paths. Shopify's own
        # `X-Shopify-Webhook-Id` is unique per delivery ATTEMPT, so an id taken
        # from it would never collide with the poll's — six deliveries would be
        # six rows, and "one effect" would silently become "one per path".
        first = await _connector(orders=[_order()]).fetch_since(TARGET, limit=250)
        second = await _connector(orders=[_order()]).fetch_since(TARGET, limit=250)

        assert first[0].external_event_id == second[0].external_event_id
        assert first[0].external_event_id == "order_paid:450789469"

    async def test_an_abandoned_checkout_is_keyed_on_its_token(self) -> None:
        (event,) = await _connector(
            checkouts=[
                {
                    "id": 9001,
                    "token": "b1a2c3",
                    "updated_at": "2026-08-06T13:00:00-03:00",
                    "phone": "+5511999990000",
                    "total_price": "89.00",
                    "currency": "BRL",
                    "line_items": [],
                }
            ]
        ).fetch_since(TARGET, limit=250)

        assert event.event_type == "checkout_abandoned"
        # The token is what `abandoned_checkout_url` carries, so it is what a
        # webhook body identifies the same checkout by.
        assert event.external_event_id == "checkout_abandoned:b1a2c3"


class TestThePhoneIsCleanedNeverGuessed:
    async def test_punctuation_goes_and_the_country_code_is_not_invented(self) -> None:
        (event,) = await _connector(
            orders=[_order(phone="(11) 99999-0000")]
        ).fetch_since(TARGET, limit=250)

        # No `+55`. A number we decided was Brazilian is a message to a person
        # nobody confirmed exists. `apply_domain_event` refuses this and leaves
        # a `failed` event a human can read — visible and wrong beats invisible
        # and confident.
        assert event.payload["phone"] == "11999990000"

    async def test_the_customers_phone_is_used_when_the_order_has_none(self) -> None:
        (event,) = await _connector(
            orders=[_order(phone=None, customer={"id": 7, "phone": "+5511988880000"})]
        ).fetch_since(TARGET, limit=250)

        assert event.payload["phone"] == "+5511988880000"


class TestTheAddressIsComposedFromTheRow:
    async def test_the_host_is_the_stores_own_and_the_token_travels_in_the_header(
        self,
    ) -> None:
        seen: list[httpx.Request] = []
        await _connector(record=seen).fetch_since(TARGET, limit=250)

        for request in seen:
            # The host is `source_account_id`, which is why this adapter names
            # no provider address: unlike Meta's one graph, Shopify's host is
            # per store, so there is nothing to hardcode.
            assert request.url.host == SHOP
            assert request.headers["X-Shopify-Access-Token"] == TOKEN
            assert DEFAULT_API_VERSION in request.url.path

    async def test_the_cursor_becomes_updated_at_min(self) -> None:
        seen: list[httpx.Request] = []
        await _connector(record=seen).fetch_since(TARGET, limit=250)

        for request in seen:
            assert request.url.params["updated_at_min"] == TARGET.cursor.isoformat()

    async def test_a_never_synced_store_asks_without_a_lower_bound(self) -> None:
        seen: list[httpx.Request] = []
        first_ever = SyncTarget(
            connector_account_id=uuid4(),
            tenant_id=uuid4(),
            platform="shopify",
            source_account_id=SHOP,
            cursor=None,
        )
        await _connector(record=seen).fetch_since(first_ever, limit=250)

        for request in seen:
            assert "updated_at_min" not in request.url.params

    async def test_an_http_error_carries_the_status_into_the_message(self) -> None:
        connector = ShopifyConnector(
            SingleTokenSecrets(source_account_id=SHOP, token=TOKEN),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(429, text="too many requests")
            ),
        )
        with pytest.raises(RuntimeError, match="HTTP 429"):
            await connector.fetch_since(TARGET, limit=250)


class TestTheEventsComeOutInOrder:
    async def test_two_endpoints_merge_into_one_ascending_sequence(self) -> None:
        # The cursor advances event by event, so a batch that is not ordered
        # would advance it past something not yet ingested.
        connector = _connector(
            orders=[_order(id=1, updated_at="2026-08-06T15:00:00-03:00")],
            checkouts=[
                {
                    "id": 2,
                    "token": "t2",
                    "updated_at": "2026-08-06T13:00:00-03:00",
                    "phone": "+5511999990000",
                    "line_items": [],
                }
            ],
        )
        events = await connector.fetch_since(TARGET, limit=250)

        assert [event.event_type for event in events] == [
            "checkout_abandoned",
            "order_paid",
        ]


class TestTheCredentialSeamIsHonestlyEmpty:
    async def test_the_vault_path_says_which_pendency_it_is_waiting_on(self) -> None:
        # E0-22. An adapter that quietly read a token from the environment here
        # would work, and working is how it would survive to production.
        with pytest.raises(ConnectorSecretUnavailable, match="E0-22"):
            await VaultConnectorSecrets().token_for(TARGET)

    async def test_one_stores_token_is_never_handed_to_another(self) -> None:
        secrets = SingleTokenSecrets(source_account_id="outra.myshopify.com", token=TOKEN)
        with pytest.raises(ConnectorSecretUnavailable, match="não de"):
            await secrets.token_for(TARGET)

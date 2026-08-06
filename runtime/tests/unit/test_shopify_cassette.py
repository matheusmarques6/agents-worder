"""The cassette played back through the real adapter — the blocking half.

`core/testes-e-cicd.md` §32: the lower levels simulate external APIs with
cassettes. This is that, and it is the only test in the milestone that checks
the adapter against a body nobody wrote to make a test pass — the fixtures in
`test_shopify_connector.py` are minimal by design, and a minimal body is
exactly the one that hides a field the real provider sends in a shape we did
not expect.

Today's cassette is honest about not being a recording (`recorded_at: null`):
Bruno's dev store and OAuth credentials, part of B-4, do not exist yet. So what
this file proves right now is that our translation is CONSISTENT with the shape
we believe Shopify has, not that Shopify has it. The contract suite is what
converts the belief into evidence — and when it does, this test starts failing
the moment the provider's real shape disagrees with ours, in the cassette's own
PR, with a human approving.

Nothing here asserts freshness. An expired cassette warns the nightly and
schedules a contract run; it never fails a gate, because a gate that fails on
the calendar fails on a Monday nobody touched the code.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from agents_runtime.connectors.port import SyncTarget
from agents_runtime.connectors.secrets import SingleTokenSecrets
from agents_runtime.connectors.shopify import DEFAULT_API_VERSION, ShopifyConnector
from tests.support import cassettes

pytestmark = pytest.mark.unit

CASSETTE = f"shopify_{DEFAULT_API_VERSION}"
SHOP = "loja-do-bruno.myshopify.com"

TARGET = SyncTarget(
    connector_account_id=uuid4(),
    tenant_id=uuid4(),
    platform="shopify",
    source_account_id=SHOP,
    cursor=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
)


@pytest.fixture
def played_back() -> ShopifyConnector:
    responses = cassettes.load(CASSETTE)["responses"]

    def handle(request: httpx.Request) -> httpx.Response:
        resource = "orders" if "orders" in request.url.path else "checkouts"
        return httpx.Response(200, json=responses[resource])

    return ShopifyConnector(
        SingleTokenSecrets(source_account_id=SHOP, token="shpat_faketoken"),
        transport=httpx.MockTransport(handle),
    )


class TestTheCassetteIsPinnedToTheVersionTheAdapterAsks:
    def test_the_file_name_carries_the_api_version(self) -> None:
        # A cassette recorded against 2026-01 says nothing about 2026-07. Naming
        # the file after the version is what makes bumping `DEFAULT_API_VERSION`
        # fail loudly (missing file) instead of quietly replaying the old shape
        # against the new adapter.
        assert cassettes.load(CASSETTE)["api_version"] == DEFAULT_API_VERSION


class TestTheRecordedBodyTranslatesEndToEnd:
    async def test_both_occasions_come_out_in_order(
        self, played_back: ShopifyConnector
    ) -> None:
        events = await played_back.fetch_since(TARGET, limit=250)

        assert [event.event_type for event in events] == [
            "checkout_abandoned",
            "order_paid",
        ]

    async def test_the_payment_carries_the_mirrors_own_keys(
        self, played_back: ShopifyConnector
    ) -> None:
        payment = next(
            event
            for event in await played_back.fetch_since(TARGET, limit=250)
            if event.event_type == "order_paid"
        )

        # `internal.mirror_order` reads exactly these names and takes each field
        # only when it is well formed — so a key we failed to map is not
        # permissive, it is silently dropped.
        order = payment.payload["order"]
        assert order["external_id"] == "450789469"
        assert order["total"] == "199.90"
        assert order["currency"] == "BRL"
        assert order["customer"]["external_id"] == "207119551"
        assert order["customer"]["name"] == "Maria Silva"
        assert order["items"][0]["sku"] == "CAM-P"

        # The order itself has no phone in this body; the customer does. A
        # payment whose phone we failed to find would be an `order_paid` the
        # router refuses, and a funnel that keeps chasing somebody who paid.
        assert payment.payload["phone"] == "+5511999990000"

    async def test_the_abandonment_is_keyed_on_the_checkout_token(
        self, played_back: ShopifyConnector
    ) -> None:
        abandonment = next(
            event
            for event in await played_back.fetch_since(TARGET, limit=250)
            if event.event_type == "checkout_abandoned"
        )

        assert abandonment.external_event_id == "checkout_abandoned:b1a2c3d4e5f6"
        # Punctuation gone, country code NOT invented — the recorded body has a
        # Brazilian-looking local number precisely because that is the case a
        # helpful adapter gets wrong.
        assert abandonment.payload["phone"] == "11988880000"


class TestTheStalenessRuleIsTheNightlysAndNotTheGates:
    def test_a_cassette_that_was_never_recorded_is_stale(self) -> None:
        # Which today's is, and it says so in the file. Stale means "schedule a
        # contract run", never "fail this PR".
        assert cassettes.is_stale(cassettes.load(CASSETTE), now=datetime.now(UTC))

    def test_a_fresh_recording_is_not(self) -> None:
        now = datetime(2026, 8, 6, tzinfo=UTC)
        assert not cassettes.is_stale({"recorded_at": now.isoformat()}, now=now)

    def test_thirty_one_days_is(self) -> None:
        now = datetime(2026, 8, 6, tzinfo=UTC)
        recorded = (now - timedelta(days=31)).isoformat()
        assert cassettes.is_stale({"recorded_at": recorded}, now=now)

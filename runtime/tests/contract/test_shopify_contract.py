"""The one suite that asks a real Shopify store — never on a gate.

`core/testes-e-cicd.md` §2: blocking suites never touch the external network.
This level exists for the questions a hand-written body cannot answer, and
right now it cannot run at all: the dev store and OAuth credentials are part of
**B-4 and have not arrived**. It skips itself, exactly like the OpenRouter
suite does without a key, so nobody is blocked by a pendency that is somebody
else's.

Four questions, and the rest of the system already assumes their answers:

  1. **Is `orders.json?financial_status=paid` still a filter Shopify honours,
     and does each order still carry `financial_status` and `updated_at`?**
     Those two fields are the adapter's paid gate and the account's cursor. If
     `updated_at` disappeared, the reconciliation would raise on every store
     instead of quietly drifting — which is the failure mode we chose, and this
     is where we find out we still need it.

  2. **Is `checkouts.json` still the abandoned-checkout endpoint, and does each
     entry still carry `token`?** The token is the idempotency key of every
     abandonment the poll ingests. If it vanished, the poll and the webhook
     would key the same checkout differently and D5's "one effect" would become
     "one per path", with every table looking correct.

  3. **Is the pinned API version still supported?** Shopify retires a version
     about a year after release and warns first, in
     `X-Shopify-API-Version-Warning`. A retired version starts answering for a
     different one silently.

  4. **Does the shape we hand to the translation still match what we recorded?**
     Answered by rewriting the cassette: the new bodies land in the working
     tree and reach main as a PR, so if the provider's new shape breaks the
     blocking replay, the break appears in the cassette's own PR with Bruno as
     the approver — never inside somebody else's change.

Running it: point `AGENTS_SHOPIFY_SHOP` at the dev store's `.myshopify.com`
domain and `AGENTS_SHOPIFY_ACCESS_TOKEN` at an Admin API token with
`read_orders` and `read_checkouts`. It is not in `pr.yml` and must never be.
"""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from agents_runtime.connectors.port import SyncTarget
from agents_runtime.connectors.secrets import single_token_from_env
from agents_runtime.connectors.shopify import DEFAULT_API_VERSION, ShopifyConnector
from tests.support import cassettes

CASSETTE = f"shopify_{DEFAULT_API_VERSION}"

pytestmark = pytest.mark.skipif(
    not (
        os.environ.get("AGENTS_SHOPIFY_SHOP")
        and os.environ.get("AGENTS_SHOPIFY_ACCESS_TOKEN")
    ),
    reason=(
        "AGENTS_SHOPIFY_SHOP / AGENTS_SHOPIFY_ACCESS_TOKEN not set — the contract "
        "suite needs the B-4 dev store, which does not exist yet"
    ),
)


def _target() -> SyncTarget:
    return SyncTarget(
        connector_account_id=uuid4(),
        tenant_id=uuid4(),
        platform="shopify",
        source_account_id=os.environ["AGENTS_SHOPIFY_SHOP"],
        # A window wide enough that a dev store with any history at all answers
        # with something, and narrow enough not to page through a year.
        cursor=datetime.now(UTC) - timedelta(days=90),
    )


def _connector() -> ShopifyConnector:
    return ShopifyConnector(single_token_from_env(), api_version=DEFAULT_API_VERSION)


async def test_the_store_still_answers_in_the_shape_the_adapter_translates() -> None:
    connector = _connector()
    try:
        events = await connector.fetch_since(_target(), limit=50)
    finally:
        await connector.aclose()

    # An empty dev store proves nothing and must not pass silently: the whole
    # point of this run is to look at a real body.
    assert events, (
        "a loja de desenvolvimento não devolveu nenhum pedido nem checkout nos "
        "últimos 90 dias — sem corpo real não há contrato verificado"
    )
    for event in events:
        # Constructing them at all already asserted the vocabulary, the id and
        # the aware instant (the port's three rules). This is the fourth thing:
        # the ordering the reconciliation's cursor depends on.
        assert event.occurred_at.tzinfo is not None
    assert events == sorted(events, key=lambda event: event.occurred_at)


async def test_the_pinned_api_version_is_not_being_retired() -> None:
    import httpx

    shop = os.environ["AGENTS_SHOPIFY_SHOP"]
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            httpx.URL(
                scheme="https",
                host=shop,
                path=f"/admin/api/{DEFAULT_API_VERSION}/shop.json",
            ),
            headers={"X-Shopify-Access-Token": os.environ["AGENTS_SHOPIFY_ACCESS_TOKEN"]},
        )

    assert response.status_code == 200, response.text[:300]
    warning = response.headers.get("X-Shopify-API-Version-Warning")
    assert not warning, (
        f"{DEFAULT_API_VERSION} está sendo aposentada ({warning}) — uma versão "
        "aposentada passa a responder por outra em silêncio"
    )


async def test_recording_the_cassette_for_the_blocking_replay() -> None:
    """Not an assertion — the recording itself, which reaches main as a PR."""
    import httpx

    shop = os.environ["AGENTS_SHOPIFY_SHOP"]
    token = os.environ["AGENTS_SHOPIFY_ACCESS_TOKEN"]
    since = (datetime.now(UTC) - timedelta(days=90)).isoformat()

    responses = {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        for resource, params in (
            ("orders", {"status": "any", "financial_status": "paid"}),
            ("checkouts", {}),
        ):
            response = await client.get(
                httpx.URL(
                    scheme="https",
                    host=shop,
                    path=f"/admin/api/{DEFAULT_API_VERSION}/{resource}.json",
                ),
                params={**params, "limit": 5, "updated_at_min": since},
                headers={"X-Shopify-Access-Token": token},
            )
            assert response.status_code == 200, response.text[:300]
            responses[resource] = response.json()

    cassettes.rerecord(CASSETTE, responses, at=datetime.now(UTC))

"""The door in refuses what the door it feeds could not act on.

`PlatformEvent` exists to be handed to `internal.ingest_webhook` unchanged —
that is D5, and it is why there is no second write path. The consequence is
that every field of this object is load-bearing somewhere the poll cannot see:
the id is half of the idempotency key, the type is what the router branches on,
the instant becomes the account's cursor. A value that is wrong here does not
fail here; it fails as a silently discarded event, or as a duplicate that ate a
real one. So it fails HERE.
"""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from agents_runtime.connectors.port import PLATFORM_EVENT_TYPES, PlatformEvent, SyncTarget


def _event(**overrides) -> PlatformEvent:
    fields = {
        "external_event_id": "1042",
        "event_type": "order_paid",
        "occurred_at": datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
        "payload": {"phone": "+5511999990000"},
    }
    return PlatformEvent(**{**fields, **overrides})


class TestTheVocabularyIsTheRoutersVocabulary:
    def test_a_translated_type_is_accepted(self) -> None:
        assert _event(event_type="checkout_abandoned").event_type == "checkout_abandoned"

    def test_the_platforms_own_spelling_is_refused(self) -> None:
        # Shopify calls it `orders/paid`. Ingested untranslated it would be
        # stored, enqueued, and then discarded by `apply_domain_event` with a
        # trail nobody reads — a poll that reports success and changes nothing.
        with pytest.raises(ValueError, match="não traduzido"):
            _event(event_type="orders/paid")

    def test_every_type_the_router_knows_is_constructible(self) -> None:
        for event_type in PLATFORM_EVENT_TYPES:
            assert _event(event_type=event_type).event_type == event_type


class TestTheIdempotencyKeyIsNeverEmpty:
    def test_an_empty_external_id_is_refused(self) -> None:
        # `UNIQUE (source, source_account_id, external_event_id)`: with an empty
        # id every event of a store collides with every other, so the second
        # one is swallowed as a duplicate. The replay proof would still pass —
        # one effect — while the data was being eaten.
        with pytest.raises(ValueError, match="external_event_id"):
            _event(external_event_id="")


class TestTheCursorCanBeCompared:
    def test_a_naive_instant_is_refused(self) -> None:
        # The stored cursor is `timestamptz`. Comparing it with a naive value
        # raises deep inside the advance, on the second poll of a store, in a
        # sweep nobody is watching.
        with pytest.raises(ValueError, match="ingênuo"):
            # Naive on purpose: `tests/**` waives DTZ precisely so a test may
            # build the value the production rule exists to refuse.
            _event(occurred_at=datetime(2026, 8, 6, 12, 0))


class TestTheTargetIsARowNotAnArgument:
    def test_a_never_polled_store_has_no_cursor(self) -> None:
        target = SyncTarget(
            connector_account_id=uuid4(),
            tenant_id=uuid4(),
            platform="shopify",
            source_account_id="loja.myshopify.com",
            cursor=None,
        )
        assert target.cursor is None

    def test_the_target_is_frozen(self) -> None:
        # The poll passes this object around; a step that could rewrite
        # `tenant_id` on it would be a step that chooses whose store it is.
        target = SyncTarget(
            connector_account_id=uuid4(),
            tenant_id=uuid4(),
            platform="shopify",
            source_account_id="loja.myshopify.com",
            cursor=None,
        )
        with pytest.raises(FrozenInstanceError):
            target.tenant_id = uuid4()  # type: ignore[misc]

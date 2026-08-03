"""The Cloud API adapter, proven against a fake transport.

`httpx.MockTransport` keeps everything in-process — no socket is opened, so
this is honestly `unit`. What CANNOT be proven here is Meta's side of the
contract (does `biz_opaque_callback_data` echo back? does a resend duplicate?)
— that is the `contract` suite, weekly, with real credentials, and it stays
pendência nº 2 until B-4 hands over a token.

The request shapes mirror worder1, which ran this API in production.
"""

import json
import uuid

import httpx
import pytest

from agents_runtime.channels.cloud_api import CloudApiChannel, from_env
from agents_runtime.channels.port import ClaimedSend
from agents_runtime.queueing.failures import Failure, classify


def a_send(**overrides) -> ClaimedSend:
    defaults = dict(
        outbox_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        channel_type="cloud",
        channel_external_id="123456789012345",  # the phone_number_id
        to_phone_e164="+5511987654321",
        payload={"text": "Seu pedido saiu para entrega 🧡"},
        idempotency_key="reply-abc-2",
        attempt_count=1,
    )
    return ClaimedSend(**{**defaults, **overrides})


def channel_answering(handler) -> CloudApiChannel:
    return CloudApiChannel("token-de-teste", transport=httpx.MockTransport(handler))


async def test_the_request_is_the_one_the_cloud_api_expects() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "wamid.HBg="}]})

    wamid = await channel_answering(handler).send(a_send())

    assert wamid == "wamid.HBg="
    # The account routes by phone_number_id in the PATH — the same id that
    # resolves the tenant on the inbound side.
    assert seen["url"].endswith("/123456789012345/messages")
    assert seen["auth"] == "Bearer token-de-teste"
    assert seen["body"]["to"] == "+5511987654321"
    assert seen["body"]["type"] == "text"
    assert seen["body"]["text"] == {"body": "Seu pedido saiu para entrega 🧡"}


async def test_the_idempotency_key_travels_in_the_opaque_field() -> None:
    # Decisão 59: one key, two worlds. Without this line in the payload, an
    # unknown outbox row whose sender died before the wamid arrived would have
    # NO evidence that could ever resolve it.
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})

    await channel_answering(handler).send(a_send(idempotency_key="reply-conv-7"))

    assert captured["biz_opaque_callback_data"] == "reply-conv-7"


@pytest.mark.parametrize(
    ("status", "expected_class"),
    [
        (429, Failure.TRANSIENT),
        (500, Failure.TRANSIENT),
        (503, Failure.TRANSIENT),
        (400, Failure.PERMANENT),
        (401, Failure.PERMANENT),
    ],
)
async def test_provider_errors_speak_the_classifier_language(
    status: int, expected_class: Failure
) -> None:
    # The adapter's error message IS its integration contract with unidade 4:
    # the classifier reads the HTTP status out of it. Same vocabulary, one
    # judge.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "nope"}})

    with pytest.raises(RuntimeError) as failure:
        await channel_answering(handler).send(a_send())

    assert classify(failure.value) is expected_class


async def test_a_2xx_without_a_wamid_is_permanent() -> None:
    # A success the provider cannot name is a contract violation — retrying it
    # blind is how a customer gets the same message twice.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": []})

    with pytest.raises(ValueError) as failure:
        await channel_answering(handler).send(a_send())

    assert classify(failure.value) is Failure.PERMANENT


async def test_an_unknown_payload_shape_never_reaches_the_wire() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    with pytest.raises(ValueError):
        await channel_answering(handler).send(a_send(payload={"template": "x"}))

    assert calls == 0, "guessing at an unknown shape would send SOMETHING to a customer"


def test_missing_credentials_die_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    # The same doctrine as a broken AGENTS_CHANNEL spec: absent and broken are
    # different states, and neither may quietly become 'no sender'.
    monkeypatch.delenv("AGENTS_META_ACCESS_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="AGENTS_META_ACCESS_TOKEN"):
        from_env("postgresql://x")

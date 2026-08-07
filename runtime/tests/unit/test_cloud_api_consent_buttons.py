"""Os botões do RF-033(a) na forma que a Cloud API espera — contra transporte falso.

`httpx.MockTransport` keeps everything in-process, so this is honestly `unit`.
What it proves is the shape we PUT ON THE WIRE; what it cannot prove is Meta's
side of it, and that division is named out loud in the report of this step
rather than left to be discovered:

  * **provable here** — that a touch carrying `buttons` becomes an
    `interactive`/`button` message, that each button becomes
    `{type: reply, reply: {id, title}}` with the id `dispatch/consent.py`
    issues, that the body text is the cadence's copy, that the opaque
    idempotency key still travels, and that a touch WITHOUT buttons is
    byte-for-byte the plain text message E1 shipped;
  * **only the `contract` suite can confirm** — that Meta accepts this shape for
    this number, that `interactive.button_reply.id` comes back verbatim, and
    (the one that can change the design) that a business-initiated message
    outside the 24-hour service window may carry reply buttons at all without
    being an approved template.

The last one is why this file asserts the free-form `interactive` shape and
nothing about templates: inventing a template payload against a template that
does not exist would be inventing API.
"""

import json
import uuid

import httpx
import pytest

from agents_runtime.channels.cloud_api import CONSENT_BUTTONS, CloudApiChannel
from agents_runtime.channels.port import ClaimedSend
from agents_runtime.dispatch import consent

pytestmark = pytest.mark.unit

TOUCH_TEXT = "Vi que ficou algo no carrinho."


def a_touch(**overrides) -> ClaimedSend:
    defaults = dict(
        outbox_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        channel_type="cloud",
        channel_external_id="123456789012345",
        to_phone_e164="+5511987654321",
        payload={
            "text": TOUCH_TEXT,
            "generated": False,
            CONSENT_BUTTONS: consent.buttons_for(consent.PENDING),
        },
        idempotency_key="touch-abc",
        attempt_count=1,
    )
    return ClaimedSend(**{**defaults, **overrides})


async def _send(send: ClaimedSend) -> dict:
    """What the adapter actually posted, with no socket involved."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})

    channel = CloudApiChannel("token-de-teste", transport=httpx.MockTransport(handler))
    await channel.send(send)
    return seen


class TestTheShapeOnTheWire:
    async def test_a_touch_with_buttons_is_an_interactive_message(self) -> None:
        body = await _send(a_touch())

        assert body["type"] == "interactive"
        assert body["interactive"]["type"] == "button"
        assert body["interactive"]["body"] == {"text": TOUCH_TEXT}

    async def test_each_button_carries_the_id_the_reply_comes_back_with(self) -> None:
        """The whole handshake: this id is what lands in
        `interactive.button_reply.id`, and `dispatch/consent.py` owns both
        ends."""
        body = await _send(a_touch())

        assert body["interactive"]["action"]["buttons"] == [
            {
                "type": "reply",
                "reply": {"id": consent.AUTHORIZE_BUTTON_ID, "title": "Autorizar"},
            },
            {"type": "reply", "reply": {"id": consent.BLOCK_BUTTON_ID, "title": "Bloquear"}},
        ]

    async def test_the_opaque_key_still_travels(self) -> None:
        """Decisão 59 does not get an exception for being a different message
        type: without it, an unknown outbox row whose sender died before the
        wamid arrived has no evidence that could ever resolve it."""
        body = await _send(a_touch(idempotency_key="touch-7"))

        assert body["biz_opaque_callback_data"] == "touch-7"

    async def test_a_touch_without_buttons_is_the_plain_text_message_it_always_was(
        self,
    ) -> None:
        """The regression that matters: every reply and every touch to a contact
        who already answered goes out unchanged."""
        body = await _send(a_touch(payload={"text": TOUCH_TEXT, "generated": False}))

        assert body["type"] == "text"
        assert body["text"] == {"body": TOUCH_TEXT}
        assert "interactive" not in body


class TestNothingIsGuessed:
    async def test_a_malformed_button_never_reaches_the_wire(self) -> None:
        """A button without an id is a control the contact can tap and nothing
        can read — the refusal that never arrives. Sending it would be worse
        than sending no buttons at all, because it looks like consent was
        offered."""
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200)

        channel = CloudApiChannel("token", transport=httpx.MockTransport(handler))

        with pytest.raises(ValueError):
            await channel.send(a_touch(payload={"text": "oi", CONSENT_BUTTONS: [{"id": "x"}]}))

        assert calls == 0


class TestTheKeyIsOneKey:
    def test_the_adapter_reads_the_key_the_dispatch_writes(self) -> None:
        """The adapter holds the key as a literal on purpose — a sender that
        imported the dispatch would be a sender that knows how content is
        composed. This comparison is what stops the two spellings from drifting
        apart in silence, and it is a test that can fail rather than an import
        that hides the coupling."""
        assert CONSENT_BUTTONS == consent.BUTTONS

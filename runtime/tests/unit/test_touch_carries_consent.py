"""RF-033(a) — todo disparo a contato novo carrega os botões, e só ele.

The buttons are decided in `dispatch/copy.py`, before the outbox, for the same
reason the copy is (D10): `message_outbox.payload` is "o conteúdo final a
enviar", and a sender that composed content would be a sender that needs a
tenant, a contact and a reason to read the database.

The negative half is the one worth stating: a contact who already answered does
NOT get asked again, and the payload they receive is byte-for-byte the one the
milestone shipped before this step. An extra key on every message would mean an
extra key on every reply of every tenant.
"""

import pytest

from agents_runtime.dispatch import consent
from agents_runtime.dispatch.copy import render

pytestmark = pytest.mark.unit

CADENCE = [{"n": 1, "delay": "PT0S", "copy_base": "Vi que ficou algo no carrinho."}]


class TestWhoIsAsked:
    def test_a_contact_who_has_not_answered_is_asked(self) -> None:
        payload = render(CADENCE, 1, opt_status="pending")

        assert payload[consent.BUTTONS] == consent.buttons_for("pending")

    @pytest.mark.parametrize("opt_status", ["authorized", "blocked", None])
    def test_anybody_else_receives_the_touch_unchanged(self, opt_status) -> None:
        payload = render(CADENCE, 1, opt_status=opt_status)

        assert payload == {"text": "Vi que ficou algo no carrinho.", "generated": False}

    def test_a_caller_that_says_nothing_asks_nothing(self) -> None:
        """The default is no buttons, so every existing caller — and every
        reactive reply — keeps the payload it had. A default that asked would
        put consent buttons on an answer to a question the contact just typed.
        """
        assert consent.BUTTONS not in render(CADENCE, 1)


class TestItIsStillDeterministic:
    def test_the_same_touch_to_the_same_contact_renders_the_same_bytes(self) -> None:
        """The idempotency key is derived from the touch, and the outbox's
        UNIQUE is the second lock on the door: a payload that varied per attempt
        would make a redelivered job a second, different message under a key
        claiming they are the same one."""
        assert render(CADENCE, 1, opt_status="pending") == render(
            CADENCE, 1, opt_status="pending"
        )

    def test_the_buttons_do_not_disturb_what_was_already_there(self) -> None:
        cadence = [
            {"n": 1, "delay": "PT0S", "copy_base": "Oi", "cta": "Finalizar",
             "template_ref": "cart_v1"}
        ]

        payload = render(cadence, 1, opt_status="pending")

        assert payload["text"] == "Oi"
        assert payload["cta"] == "Finalizar"
        assert payload["template_ref"] == "cart_v1"
        assert payload["generated"] is False

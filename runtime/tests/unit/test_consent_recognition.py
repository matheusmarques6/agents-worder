"""RF-033(a) — o reconhecimento do botão é uma consulta, nunca um julgamento.

This is the one place in the product where a message written by a stranger
changes a permission, which makes it the place least allowed to ask a model what
the message meant (`CLAUDE.md`, trust boundaries). So the input is not prose: it
is an id WE issued, echoed back by the platform, matched against a table of two
entries.

The suite has three jobs:

  * **the handshake closes.** The ids the touch emits are the ids the reply is
    matched against. Nothing else in the codebase can fail if those two drift
    apart — a button rendered as `consent_block` and recognised as
    `block_consent` looks perfect in review and silently never blocks anybody;
  * **ordinary language is not a tap.** "Não quero mais receber nada" is an
    opt-out and this function says nothing about it, on purpose: that sentence
    is via (c), where a model detects it and `record_optout` performs it. The
    two vias must not overlap, or the deterministic half would start guessing;
  * **the row was built from a third party's payload**, so every shape it could
    arrive in is simply "not a tap" — never coerced into one.
"""

import pytest

from agents_runtime.dispatch import consent

pytestmark = pytest.mark.unit


def _tap(button_id: str) -> dict:
    """The content shape the ingestion writes for a button reply.

    Written out in full rather than built by a helper because it IS the
    contract with `supabase/functions/ingest-meta/index.ts`: this literal and
    that mapping are the same thing, and the day one changes the other has to.
    """
    return {
        "type": "interactive",
        "text": None,
        "button_reply": {"id": button_id, "title": "qualquer coisa"},
    }


class TestTheTapIsRecognisedByItsId:
    def test_bloquear_is_a_block(self) -> None:
        assert consent.recognize(_tap(consent.BLOCK_BUTTON_ID)) == consent.BLOCK

    def test_autorizar_is_an_authorisation(self) -> None:
        assert consent.recognize(_tap(consent.AUTHORIZE_BUTTON_ID)) == consent.AUTHORIZE

    def test_the_title_is_never_what_decides(self) -> None:
        """The title is display text — translatable, editable, and echoed from
        whatever the platform rendered. Deciding on it would make consent depend
        on a string a future copy edit is allowed to change."""
        content = _tap(consent.BLOCK_BUTTON_ID)
        content["button_reply"]["title"] = "Autorizar"

        assert consent.recognize(content) == consent.BLOCK


class TestWhatIsNotATap:
    def test_an_ordinary_message_says_nothing_about_consent(self) -> None:
        assert consent.recognize({"type": "text", "text": "oi, chegou meu pedido?"}) is None

    def test_a_sentence_that_MEANS_an_opt_out_is_still_not_a_tap(self) -> None:
        """Via (c), not via (a). Reading intention out of language is the
        model's job and `record_optout` is its hands; this function guessing at
        it would put a keyword list in charge of somebody's consent — and a
        keyword list is wrong in both directions."""
        content = {"type": "text", "text": "não quero mais receber nada de vocês"}

        assert consent.recognize(content) is None

    def test_an_id_we_never_issued_is_not_a_tap(self) -> None:
        assert consent.recognize(_tap("consent_unsubscribe_everything")) is None

    @pytest.mark.parametrize(
        "content",
        [
            None,
            {},
            {"button_reply": None},
            {"button_reply": "consent_block"},
            {"button_reply": {}},
            {"button_reply": {"id": None}},
            {"button_reply": {"id": ["consent_block"]}},
            {"button_reply": {"title": "Bloquear"}},
        ],
        ids=[
            "no content at all",
            "empty content",
            "a null reply",
            "a reply that is a string",
            "a reply with no id",
            "a null id",
            "an id that is a list",
            "a title without an id",
        ],
    )
    def test_a_shape_it_cannot_read_is_not_a_tap(self, content) -> None:
        """The webhook doctrine one layer in: the row was built from a third
        party's payload, and "use what parsed" is exactly how a malformed
        message becomes a permission change."""
        assert consent.recognize(content) is None


class TestTheButtonsATouchCarries:
    def test_a_contact_who_has_not_answered_gets_both(self) -> None:
        assert consent.buttons_for("pending") == [
            {"id": consent.AUTHORIZE_BUTTON_ID, "title": "Autorizar"},
            {"id": consent.BLOCK_BUTTON_ID, "title": "Bloquear"},
        ]

    def test_autorizar_comes_first(self) -> None:
        """Not cosmetic: a destructive choice offered first is a destructive
        choice taken by accident."""
        first, second = consent.buttons_for(consent.PENDING)

        assert first["title"] == "Autorizar"
        assert second["title"] == "Bloquear"

    @pytest.mark.parametrize("opt_status", ["authorized", "blocked", None, ""])
    def test_anybody_else_is_not_asked_again(self, opt_status) -> None:
        """`authorized` already answered. `blocked` never receives a proactive
        at all — the ladder stops it before this — so asking again would be a
        question nobody hears."""
        assert consent.buttons_for(opt_status) is None

    def test_a_title_fits_a_whatsapp_reply_button(self) -> None:
        """20 characters is the platform's cap, and a title over it is rejected
        by the API at send time — which is to say, after the touch is already
        committed to the outbox."""
        for button in consent.buttons_for(consent.PENDING):
            assert len(button["title"]) <= 20


class TestTheHandshakeCloses:
    def test_every_button_this_module_emits_is_one_it_recognises(self) -> None:
        """The assertion this file exists for. Emitting and recognising are two
        halves of one agreement, and no other test in the codebase fails when
        they stop matching."""
        for button in consent.buttons_for(consent.PENDING):
            assert consent.recognize(_tap(button["id"])) is not None

    def test_the_two_buttons_do_not_mean_the_same_thing(self) -> None:
        emitted = consent.buttons_for(consent.PENDING)
        decisions = {consent.recognize(_tap(button["id"])) for button in emitted}

        assert decisions == {consent.AUTHORIZE, consent.BLOCK}

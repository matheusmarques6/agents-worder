"""What a due touch says — and where it is not allowed to come from.

The content of a proactive touch is the merchant's approved cadence and nothing
else. Two properties matter more than the mapping itself:

* **determinism.** The same cadence and the same touch number produce the same
  bytes, because the idempotency key is derived from the touch and the outbox's
  UNIQUE is the second lock on the door — a payload that varied per attempt
  would make a redelivered job a second, different message under a key that
  claims they are the same one;
* **a broken cadence is loud.** An entry this module cannot read raises, so the
  job climbs to the DLQ where a human sees it. Cancelling the touch instead
  would file our own configuration fault under a contact-protection reason and
  quietly poison the only metric that diagnoses this step (S11: cancelled BY
  REASON).
"""

import pytest

from agents_runtime.dispatch.copy import CadenceMissing, render

pytestmark = pytest.mark.unit

CADENCE = [
    {"n": 1, "delay": "PT0S", "copy_base": "Vi que ficou algo no carrinho."},
    {"n": 2, "delay": "PT24H", "copy_base": "Ainda dá tempo de concluir.", "cta": "Finalizar"},
]


class TestTheCopyComesFromTheCadence:
    def test_the_touch_number_selects_its_entry(self) -> None:
        assert render(CADENCE, 1)["text"] == "Vi que ficou algo no carrinho."
        assert render(CADENCE, 2)["text"] == "Ainda dá tempo de concluir."

    def test_the_order_of_the_list_does_not_decide_which_touch_fires(self) -> None:
        # `n` identifies the touch; a cadence saved out of order is still the
        # same cadence, and `scheduled_touches.touch_number` is what we hold.
        assert render(list(reversed(CADENCE)), 1)["text"] == "Vi que ficou algo no carrinho."

    def test_the_call_to_action_travels_with_the_text(self) -> None:
        assert render(CADENCE, 2)["cta"] == "Finalizar"

    def test_a_template_reference_travels_alongside_the_text(self) -> None:
        cadence = [{"n": 1, "delay": "PT0S", "copy_base": "Oi", "template_ref": "cart_v1"}]

        assert render(cadence, 1)["template_ref"] == "cart_v1"


class TestItIsDeterministic:
    def test_the_same_touch_renders_the_same_payload_twice(self) -> None:
        assert render(CADENCE, 1) == render(CADENCE, 1)

    def test_the_payload_records_that_no_model_wrote_it(self) -> None:
        # D3c: the audit that separates generated copy from approved template
        # cannot be written retroactively. Variation by LLM is S7; until then
        # this flag is false and says so on every row.
        assert render(CADENCE, 1)["generated"] is False


class TestABrokenCadenceIsLoud:
    def test_a_touch_number_the_cadence_does_not_have_raises(self) -> None:
        # The funnel was edited after the touch was scheduled — a configuration
        # fault, not a protection.
        with pytest.raises(CadenceMissing):
            render(CADENCE, 3)

    def test_an_entry_without_text_raises(self) -> None:
        # Text is what every adapter can deliver today; the Cloud API adapter
        # rejects an outbox payload without it, and finding that out in the
        # sender would mean the touch was already committed.
        with pytest.raises(CadenceMissing):
            render([{"n": 1, "delay": "PT0S", "template_ref": "cart_v1"}], 1)

    def test_an_entry_whose_text_is_blank_raises(self) -> None:
        with pytest.raises(CadenceMissing):
            render([{"n": 1, "delay": "PT0S", "copy_base": "   "}], 1)

    def test_an_empty_cadence_raises(self) -> None:
        with pytest.raises(CadenceMissing):
            render([], 1)

    def test_an_entry_that_is_not_an_object_is_not_the_touch_we_are_firing(self) -> None:
        # The backend never trusts a shape, even one the schema's CHECK makes
        # unreachable for a funnel saved today.
        with pytest.raises(CadenceMissing):
            render(["não é um toque"], 1)  # type: ignore[list-item]

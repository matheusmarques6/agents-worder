"""E2 · S4 — the think-gate, the sixth rule of the `agent_core` row.

Extended reasoning is the expensive path: it costs tokens and latency on every
single reply. The gate decides, BEFORE the model is called, whether the pending
input deserves it — a "oi" does not, a complaint with three questions does.

It is a pure function returning a decision AND its reason. The reason is data,
not a log line: the eval harness (S3) and the cost analysis (S10) both need to
say why a conversation took the expensive path, and a string produced by a
`print` would reach neither.

Definition fixed with Bruno on 2026-08-03 — no canonical doc defined it, only
named it (arquitetura §3, testes-e-cicd §2).
"""

import pytest

from agents_runtime.agent_core.think_gate import PendingMessage, should_think


def messages(*texts: str) -> tuple[PendingMessage, ...]:
    return tuple(PendingMessage(author="contact", text=text) for text in texts)


# --- the cheap path -----------------------------------------------------------


@pytest.mark.parametrize("text", ["oi", "Olá!", "obrigado 🧡", "bom dia", "ok"])
def test_a_greeting_does_not_deserve_extended_reasoning(text: str) -> None:
    decision = should_think(messages(text))

    assert decision.think is False


def test_one_short_question_does_not_deserve_it_either() -> None:
    decision = should_think(messages("vocês entregam em Curitiba?"))

    assert decision.think is False


# --- the expensive path -------------------------------------------------------


def test_several_questions_in_one_message_turn_it_on() -> None:
    decision = should_think(
        messages("vocês entregam em Curitiba? qual o prazo? dá pra parcelar?")
    )

    assert decision.think is True
    assert decision.reason == "multiple_questions"


def test_a_complaint_turns_it_on() -> None:
    decision = should_think(messages("meu pedido veio quebrado, quero reembolso"))

    assert decision.think is True
    assert decision.reason == "friction"


def test_many_coalesced_messages_turn_it_on() -> None:
    # The debounce hands over everything that arrived in the window. Four
    # fragments are a train of thought the model has to reconcile.
    decision = should_think(messages("oi", "então", "sobre aquele pedido", "e o frete"))

    assert decision.think is True
    assert decision.reason == "many_messages"


def test_a_long_message_turns_it_on() -> None:
    decision = should_think(messages("preciso de ajuda. " * 40))

    assert decision.think is True
    assert decision.reason == "long_message"


# --- the shape of the decision ------------------------------------------------


def test_the_reason_is_data_even_on_the_cheap_path() -> None:
    # Never an empty string: "why did this NOT think?" is a question the cost
    # analysis asks as often as the opposite one.
    decision = should_think(messages("oi"))

    assert decision.reason == "simple_input"


def test_friction_wins_over_the_cheap_signals() -> None:
    # A short complaint is still a complaint. Order of evaluation is a rule, not
    # an accident of the ifs.
    decision = should_think(messages("cancela"))

    assert decision.think is True
    assert decision.reason == "friction"


def test_only_the_contacts_own_messages_are_weighed() -> None:
    # The agent's own turns are in the window too. Counting them would let a
    # chatty agent talk itself into the expensive path.
    pending = (
        PendingMessage(author="agent", text="Posso ajudar?"),
        PendingMessage(author="agent", text="Está aí?"),
        PendingMessage(author="agent", text="Qualquer coisa me chame."),
        PendingMessage(author="contact", text="oi"),
    )

    decision = should_think(pending)

    assert decision.think is False


def test_no_pending_message_is_rejected() -> None:
    # There is nothing to answer, so there is no decision to make — reaching
    # here means the caller skipped the staleness check.
    with pytest.raises(ValueError):
        should_think(())

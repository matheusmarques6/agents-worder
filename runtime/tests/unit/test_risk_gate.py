"""The risk gate — which sent replies are worth auditing after the fact.

Decision 92: auditing follows risk, not the coin. Random sampling would treat
"obrigado, até mais" and "seu reembolso de R$ 340 cai em 3 dias úteis" as
equally likely to be wrong; they are not.

Every test here asserts the reason as a literal string rather than importing
the module's own vocabulary — the S4 lesson: a test that cites the constant it
verifies inverts with it and proves nothing.
"""

import pytest

from agents_runtime.judges.risk_gate import SentReply, assess_risk

pytestmark = pytest.mark.unit


def _courtesy(**overrides) -> SentReply:
    """A reply with nothing at stake, so each test adds exactly one signal."""
    return SentReply(text="Imagina! Qualquer coisa é só chamar. 🧡", **overrides)


def test_a_courtesy_reply_is_not_worth_a_judge() -> None:
    decision = assess_risk(_courtesy())

    assert decision.evaluate is False
    assert decision.reasons == ()


def test_a_reply_that_quotes_money_is_audited() -> None:
    decision = assess_risk(
        SentReply(text="O reembolso de R$ 340,00 já foi solicitado para você.")
    )

    assert decision.evaluate is True
    assert "money" in decision.reasons


def test_a_reply_that_promises_a_deadline_is_audited() -> None:
    decision = assess_risk(SentReply(text="Seu pedido chega em até 3 dias úteis."))

    assert decision.evaluate is True
    assert "deadline" in decision.reasons


def test_a_reply_that_commits_the_store_is_audited() -> None:
    decision = assess_risk(
        SentReply(text="Pode deixar que eu troco o produto por outro tamanho.")
    )

    assert decision.evaluate is True
    assert "commitment" in decision.reasons


def test_a_reply_grounded_on_retrieved_knowledge_is_audited() -> None:
    """An answer built on the merchant's FAQ is an assertion about the catalogue —
    the shape alucination takes here, and it carries no number to catch."""
    decision = assess_risk(_courtesy(used_knowledge=True))

    assert decision.evaluate is True
    assert "grounded_claim" in decision.reasons


def test_a_reply_the_judge_already_flagged_is_audited() -> None:
    """Below a perfect score at least one criterion failed. The reply still went
    out (only `critical` blocks — decisão 87), but the gate already hesitated
    once, and that is evidence, not a threshold someone invented."""
    decision = assess_risk(_courtesy(judge_score=0.9))

    assert decision.evaluate is True
    assert "judge_flagged" in decision.reasons


def test_a_reply_that_needed_a_second_attempt_is_audited() -> None:
    decision = assess_risk(_courtesy(judge_score=1.0, judge_attempts=2))

    assert decision.evaluate is True
    assert "judge_flagged" in decision.reasons


def test_a_reply_written_into_friction_is_audited() -> None:
    """The think-gate already read the contact as a complaint. A bad reply to an
    irritated customer is the expensive kind of bad."""
    decision = assess_risk(_courtesy(think_reason="friction"))

    assert decision.evaluate is True
    assert "friction" in decision.reasons


def test_the_decision_carries_every_reason_it_found() -> None:
    """Not the first match: the auditor wants to know everything at stake in the
    reply, which is where this parts ways with the think-gate's precedence."""
    decision = assess_risk(
        SentReply(
            text="Pode deixar: devolvemos os R$ 340,00 em até 5 dias úteis.",
            used_knowledge=True,
        )
    )

    assert decision.evaluate is True
    assert set(decision.reasons) >= {"money", "deadline", "commitment", "grounded_claim"}


def test_a_perfect_score_is_not_a_reason_by_itself() -> None:
    """Guards the inverse of the flagged case: a clean judgement must not drag
    every courtesy reply into the expensive path."""
    decision = assess_risk(_courtesy(judge_score=1.0, judge_attempts=1))

    assert decision.evaluate is False
    assert decision.reasons == ()

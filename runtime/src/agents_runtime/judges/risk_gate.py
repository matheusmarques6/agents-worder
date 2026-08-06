"""The risk gate — does this SENT reply deserve an expensive audit?

The sibling of the think-gate (S4), one turn later. The think-gate decides,
before the model runs, whether the incoming window deserves expensive
reasoning; this one decides, after the reply is out, whether it deserves an
expensive second judge. Both are pure — no clock, no randomness, no I/O — and
both carry their reason as DATA, because "why was this conversation audited?"
is a question the hub and S10 ask routinely.

It lives in `judges/` rather than next to the think-gate because what it gates
is the post-hoc judge, not the reply.

Decision 92, redesigned by Bruno on 2026-08-06. The first design sampled a
random fraction outside the shadow window; he refused it, and rightly — a
lottery treats "obrigado, até mais" and "seu reembolso de R$ 340 cai em 3 dias
úteis" as equally likely to be wrong. Auditing follows risk, not the coin.

**It gates auditing, never sending.** Judge 1 pre-send still runs on 100% of
replies (S8). A false negative here costs measurement, never protection —
which is exactly what lets the signals below be heuristic. It errs towards
auditing for the same reason: a needless audit costs half a cent, a missed one
costs a blind spot.

The blind spot it does have — a false claim carrying no number, date or promise
("sim, esse tênis é impermeável") — is closed by the calibration batch that
runs the judge over what this gate SKIPPED, not by a sampling floor. A gate
never confronted with what it lets through is decoration.
"""

import re
from dataclasses import dataclass

# Money on the line: the reply quotes an amount, a discount or a payment. A
# wrong number here is the error that becomes a complaint and a refund.
MONEY_TERMS = (
    "r$",
    "reais",
    "preço",
    "preco",
    "valor",
    "custa",
    "desconto",
    "cupom",
    "frete",
    "parcel",
    "boleto",
    "pix",
    "grátis",
    "gratis",
    "%",
)

# A promise about time the store now has to keep.
DEADLINE_TERMS = (
    "dias",
    "dia útil",
    "dia util",
    "hoje",
    "amanhã",
    "amanha",
    "semana",
    "prazo",
    "horas",
    "minutos",
    "segunda-feira",
    "terça",
    "terca",
    "quarta-feira",
    "quinta",
    "sexta",
    "sábado",
    "sabado",
    "domingo",
)

#: 12/05, 3/8 — a date written as digits, which no term list would catch.
DATE_PATTERN = re.compile(r"\b\d{1,2}/\d{1,2}\b")

# The agent binding the merchant to an action. First person on purpose: "o
# prazo é de 3 dias" states a policy, "eu troco pra você" promises a deed.
COMMITMENT_TERMS = (
    "pode deixar",
    "pode contar",
    "eu troco",
    "vou trocar",
    "trocamos",
    "devolvemos",
    "vou devolver",
    "reembolsamos",
    "vou reembolsar",
    "garanto",
    "prometo",
    "asseguro",
    "resolvo",
    "vou resolver",
    "cuido disso",
    "enviaremos",
    "vou enviar",
    "faremos",
    "vou fazer",
)

PERFECT_SCORE = 1.0
"""Below this, at least one criterion failed. The reply still went out — only
`critical` blocks (decisão 87) — but the portão hesitated once, and hesitation
is evidence rather than a threshold someone invented. The 0,85 floor of D3 is
NOT usable here: it belongs to the activation gate, over a pack (decisão 87)."""

FRICTION = "friction"
"""The think-gate's reading of the contact, carried through. A bad reply to an
irritated customer is the expensive kind of bad."""


@dataclass(frozen=True, slots=True)
class SentReply:
    """What the gate weighs: the reply the customer received, plus the facts the
    turn already recorded about how it was produced.

    Only the AGENT's text — never the contact's message. What a stranger wrote
    is not evidence about whether the store's answer was right.
    """

    text: str
    used_knowledge: bool = False
    judge_score: float | None = None
    judge_attempts: int = 1
    think_reason: str = "simple_input"


@dataclass(frozen=True, slots=True)
class RiskDecision:
    evaluate: bool
    reasons: tuple[str, ...]


def assess_risk(reply: SentReply) -> RiskDecision:
    """A sent reply → whether to pay for a post-hoc judge, and everything that
    made it worth paying for.

    Every reason found, not the first: the think-gate answers "why did this cost
    extra" (one cause explains a bill), while an auditor wants the whole list of
    what was at stake.
    """
    lowered = reply.text.lower()
    reasons: list[str] = []

    if any(term in lowered for term in MONEY_TERMS):
        reasons.append("money")

    if any(term in lowered for term in DEADLINE_TERMS) or DATE_PATTERN.search(lowered):
        reasons.append("deadline")

    if any(term in lowered for term in COMMITMENT_TERMS):
        reasons.append("commitment")

    # An answer built on the merchant's own FAQ is an assertion about the
    # catalogue — the shape a hallucination takes here, and the one that carries
    # no number for the lists above to catch.
    if reply.used_knowledge:
        reasons.append("grounded_claim")

    if (reply.judge_score is not None and reply.judge_score < PERFECT_SCORE) or (
        reply.judge_attempts > 1
    ):
        reasons.append("judge_flagged")

    if reply.think_reason == FRICTION:
        reasons.append(FRICTION)

    # The decision IS the reasons: nothing at stake, nothing to audit.
    return RiskDecision(evaluate=bool(reasons), reasons=tuple(reasons))

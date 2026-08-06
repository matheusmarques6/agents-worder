"""Anti-ban copy variation, and the gate that stands where the judge does not.

**Read this before changing anything here.** D3 of the E3 plan: Bruno decided
that Judge 1 belongs to real time — a reactive reply is judged, a funnel touch
and a campaign are not. That decision leaves exactly one place in the product
where text a model wrote reaches a contact without an LLM gate in front of it,
and this module is what occupies the empty chair.

So the rule is deliberately narrow and deliberately deterministic: the variation
may only VARY the `copy_base` a human approved. It may not introduce a **number**,
a **deadline**, a **link** or a **promise** that the base does not already carry.
A violation is not a warning — the touch does not go out and a row is opened in
`alerts` (D3b).

Why those four families and not "is this a good message": a deterministic gate can
check FORM, never intention, and pretending otherwise would be the worse failure.
Those four are the forms that turn a recovery message into a liability — a price
nobody quoted, a deadline nobody promised, a link nobody controls, a discount
nobody approved. That the gate confers form and not intention is the residual risk
D3 records and Bruno accepted, and it belongs in a docstring rather than in a
plan nobody rereads.

The rules are STRICT on purpose. A false positive suppresses one touch and raises
one alert; a false negative puts a number a store never agreed to on a customer's
phone. Only one of those two directions is recoverable.
"""

import re
import unicodedata
from collections.abc import Awaitable, Callable

#: Every violation this module can report, as data. In the shape of
#: `ladder.DENIAL_REASONS` and for the same reason: what blocks a send is read by
#: an operator months later, and a reason invented at the call site is a metric
#: bucket nothing can group.
VIOLATIONS = (
    "empty",
    "introduced_number",
    "introduced_deadline",
    "introduced_link",
    "introduced_promise",
    "repeats_previous",
)

#: How many times a rejected variation may be asked for again before the touch
#: is given up on. Two, the same ceiling Judge 1 gets pre-send (`CLAUDE.md`) —
#: the number is the same because the situation is: a generator that produced
#: something unusable twice is not one more call away from producing something
#: usable, and each extra call is money and latency spent on a message that is
#: already late.
REGENERATION_LIMIT = 2

_TOKEN = re.compile(r"[a-z0-9]+")
_DIGITS = re.compile(r"\d[\d.,:/]*")

#: A link is anything that could be typed into a browser. Deliberately generous:
#: a domain the base never mentioned is the fastest way to make our own message
#: indistinguishable from a phishing one.
_LINK = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b[\w-]+\.(?:com|com\.br|br|net|org|io|me|app|shop|store|link|xyz)\b\S*",
    re.IGNORECASE,
)

#: Numbers a model spells out instead of writing. Without these the gate reads
#: "vinte e quatro horas" as prose and lets through the deadline it refused in
#: the form "24h" — a hole that only a Portuguese-speaking reader would spot.
_SPELLED_NUMBERS = frozenset(
    """zero um uma dois duas tres quatro cinco seis sete oito nove dez onze doze
    treze quatorze catorze quinze dezesseis dezessete dezoito dezenove vinte
    trinta quarenta cinquenta sessenta setenta oitenta noventa cem cento
    duzentos trezentos quinhentos mil milhao milhoes meia metade dobro triplo
    primeiro segunda terceiro dezena dezenas dúzia duzia""".split()
)

#: Time that COMMITS us to something. Not every temporal word — "quando", "hoje
#: em dia" and friends are prose. These are the ones that turn a message into a
#: deadline the store never agreed to honour.
_DEADLINE_TERMS = frozenset(
    """hoje amanha ontem agora ja ainda prazo prazos expira expiram expirando
    vence vencem vencendo acaba acabam acabando termina terminam terminando
    ultimas ultimos ultima ultimo restam restante faltam falta minuto minutos
    hora horas dia dias semana semanas mes meses ano anos madrugada manha tarde
    noite segunda terca quarta quinta sexta sabado domingo imediatamente
    urgente urgentemente""".split()
)

#: What a message may not start offering on its own. A discount, a freebie or a
#: guarantee is a commercial decision; a model reaching for one to make the copy
#: warmer is a model spending the merchant's margin.
_PROMISE_TERMS = frozenset(
    """gratis gratuito gratuita gratuitamente desconto descontos cupom cupons
    promocao promocoes oferta ofertas garantia garantido garantida garantimos
    garanto prometo prometemos promessa reembolso reembolsamos cashback brinde
    brindes bonus presente presentes frete fretes devolucao devolucoes troca
    trocas exclusivo exclusiva especial parcelamos parcelado juros""".split()
)


class CopyRejected(RuntimeError):
    """The variation broke the rule and the touch does not go out.

    A RuntimeError and not a protection outcome, deliberately. A rejected
    variation is OUR generator failing, not the contact being protected — the
    same reading `copy.CadenceMissing` already has. Filing it as a
    `cancel_reason` would grow the "cancelled BY REASON" metric of S11 a bucket
    that means "our bug", which is exactly the confusion that metric exists to
    prevent.
    """

    def __init__(self, violations: tuple[str, ...], variant: str) -> None:
        super().__init__(f"variação de copy rejeitada: {', '.join(violations)}")
        self.violations = violations
        self.variant = variant


def validate(base: str, variant: str, *, previous: str | None = None) -> tuple[str, ...]:
    """Every rule the variant broke, in the order of `VIOLATIONS`. Empty = it may go.

    Pure, and total: no clock, no randomness, no I/O, no exception. Same three
    strings in, same tuple out, for ever — which is what makes it a gate a test
    can hold to account rather than a heuristic that drifts.
    """
    broken: list[str] = []

    if not variant.strip():
        # An empty variation is not a safe one: it would put a blank message on
        # somebody's phone under the store's name.
        return ("empty",)

    base_words = _words(base)
    variant_words = _words(variant)

    base_numbers = _numbers(base)
    if _numbers(variant) - base_numbers:
        broken.append("introduced_number")
    if (variant_words & _SPELLED_NUMBERS) - (base_words & _SPELLED_NUMBERS):
        # A spelled number the base does not spell IS a new number, even when the
        # base carries the same quantity in digits: we cannot tell "vinte" from
        # "20" without arithmetic, and a gate that guesses is not a gate.
        broken.append("introduced_number")

    if (variant_words & _DEADLINE_TERMS) - (base_words & _DEADLINE_TERMS):
        broken.append("introduced_deadline")

    if _links(variant) - _links(base):
        broken.append("introduced_link")

    if (variant_words & _PROMISE_TERMS) - (base_words & _PROMISE_TERMS):
        broken.append("introduced_promise")

    if previous is not None and _same_message(variant, previous):
        # `CLAUDE.md`, Evolution anti-ban: "copy never repeats the last one".
        # Compared after normalisation because a provider that sees the same
        # text twice does not care about our punctuation.
        broken.append("repeats_previous")

    return tuple(reason for reason in VIOLATIONS if reason in broken)


async def vary(
    base: str,
    *,
    generate: Callable[[str], Awaitable[str]],
    previous: str | None = None,
    attempts: int = REGENERATION_LIMIT,
) -> str:
    """Ask for a variation of `base` until one passes the gate, or give up loudly.

    `generate` is injected — this module never knows what a model is, which is
    what lets the whole rule close against a stand-in with no API key, exactly
    as every other LLM path in E2 does.

    Raises `CopyRejected` when the budget runs out. The caller opens the alert:
    writing rows is not a pure module's job, and the touch that does not go out
    is the caller's touch.
    """
    last = CopyRejected(("empty",), "")
    for _ in range(max(attempts, 1)):
        variant = await generate(base)
        violations = validate(base, variant, previous=previous)
        if not violations:
            return variant
        last = CopyRejected(violations, variant)
    raise last


def _fold(text: str) -> str:
    """Lowercase, without accents. `Grátis` and `gratis` are the same promise,
    and a gate that could be dodged by an acute accent is decoration."""
    stripped = unicodedata.normalize("NFD", text.lower())
    return "".join(char for char in stripped if unicodedata.category(char) != "Mn")


def _words(text: str) -> frozenset[str]:
    return frozenset(_TOKEN.findall(_fold(text)))


def _numbers(text: str) -> frozenset[str]:
    """Digit runs, with their trailing punctuation trimmed. `R$ 49,90` and
    `49,90` are the same quantity; `49,90.` is too."""
    return frozenset(match.group().rstrip(".,:/") for match in _DIGITS.finditer(text))


def _links(text: str) -> frozenset[str]:
    return frozenset(match.group().rstrip(".,;)").lower() for match in _LINK.finditer(text))


def _same_message(one: str, other: str) -> bool:
    return _collapse(one) == _collapse(other)


def _collapse(text: str) -> str:
    return " ".join(_TOKEN.findall(_fold(text)))

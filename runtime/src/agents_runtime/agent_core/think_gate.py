"""The think-gate — does this input deserve extended reasoning?

Extended reasoning costs tokens and latency on every reply, so the decision is
made before the model is called, from the pending input alone. Pure: no clock,
no randomness, no I/O — the same messages always produce the same decision,
which is what lets the eval harness compare two agent versions at all.

The decision carries its reason as DATA. "Why did this conversation take the
expensive path?" is a question S10's cost analysis asks routinely, and an
answer that only existed in a log line would never reach it.

Definition fixed with Bruno on 2026-08-03: the canonical docs name the gate
(arquitetura §3, testes-e-cicd §2) but never defined it.
"""

from dataclasses import dataclass

# Words that mean the conversation has friction: a complaint, a cancellation,
# money going backwards, or an escalation. These deserve the careful path even
# in three characters — "cancela" is short and expensive to get wrong.
FRICTION_TERMS = (
    "reclama",
    "reembols",
    "estorn",
    "cancel",
    "devolv",
    "troca",
    "quebrad",
    "defeit",
    "atras",
    "não chegou",
    "nao chegou",
    "não funciona",
    "nao funciona",
    "procon",
    "advogad",
    "processo",
    "golpe",
    "fraude",
    "absurdo",
    "péssim",
    "pessim",
)

MANY_MESSAGES = 3
"""Fragments from the debounce window. Above this the model is reconciling a
train of thought rather than answering one message."""

LONG_MESSAGE_CHARS = 400
"""A single message this long is a story, a spec or a complaint — never a
greeting."""

MULTIPLE_QUESTIONS = 2


@dataclass(frozen=True, slots=True)
class PendingMessage:
    author: str
    text: str


@dataclass(frozen=True, slots=True)
class ThinkDecision:
    think: bool
    reason: str


def should_think(pending: tuple[PendingMessage, ...]) -> ThinkDecision:
    """The pending window → whether to pay for extended reasoning, and why."""
    if not pending:
        raise ValueError(
            "think-gate sem mensagem pendente: não há o que responder — "
            "a checagem de obsolescência deveria ter parado antes daqui"
        )

    # Only what the contact said. The agent's own turns are in the window too,
    # and counting them would let a chatty agent talk itself into the expensive
    # path.
    from_contact = [message for message in pending if message.author == "contact"]
    if not from_contact:
        return ThinkDecision(think=False, reason="nothing_from_the_contact")

    joined = " ".join(message.text for message in from_contact).lower()

    # Friction first, and deliberately so: a short complaint is still a
    # complaint, and the cheap signals below would otherwise wave it through.
    if any(term in joined for term in FRICTION_TERMS):
        return ThinkDecision(think=True, reason="friction")

    if len(from_contact) >= MANY_MESSAGES:
        return ThinkDecision(think=True, reason="many_messages")

    if any(len(message.text) >= LONG_MESSAGE_CHARS for message in from_contact):
        return ThinkDecision(think=True, reason="long_message")

    if joined.count("?") >= MULTIPLE_QUESTIONS:
        return ThinkDecision(think=True, reason="multiple_questions")

    return ThinkDecision(think=False, reason="simple_input")

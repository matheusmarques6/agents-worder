"""Unidade 4 — limites por fila e a saída para a DLQ.

A tabela é 5 · 5 · 3 · 2 (`CLAUDE.md`), e ela é dirigida por dados de
propósito: um `if fila == ...` espalhado pelo consumidor é como uma quinta fila
nasce sem limite nenhum.

O que esta unidade decide é só isso: repete com que espera, ou desiste para
qual DLQ. Quem executa é o laço, e ele chega depois.
"""

from datetime import timedelta

import pytest

from agents_runtime.config import QueueingConfig
from agents_runtime.queueing import DOMAIN_EVENTS, EVALS, INBOUND, SCHEDULED
from agents_runtime.queueing.failures import Failure
from agents_runtime.queueing.retries import DeadLetter, Retry, decide
from tests.support.randomness import FixedRandomness

CONFIG = QueueingConfig()
CENTRED = FixedRandomness(position=0.5)


def outcome(queue: str, attempt: int, failure: Failure = Failure.TRANSIENT):
    return decide(queue, attempt=attempt, failure=failure, config=CONFIG, randomness=CENTRED)


@pytest.mark.parametrize(
    ("queue", "limit"),
    [(INBOUND, 5), (DOMAIN_EVENTS, 5), (SCHEDULED, 3), (EVALS, 2)],
)
def test_each_queue_retries_up_to_its_own_limit(queue: str, limit: int) -> None:
    assert isinstance(outcome(queue, limit), Retry)
    assert isinstance(outcome(queue, limit + 1), DeadLetter)


@pytest.mark.parametrize(
    ("queue", "dead_letter"),
    [
        (INBOUND, "q_inbound_dlq"),
        (DOMAIN_EVENTS, "q_domain_events_dlq"),
        (SCHEDULED, "q_scheduled_dlq"),
        (EVALS, "q_evals_dlq"),
    ],
)
def test_a_message_goes_to_the_dead_letter_queue_of_its_own_queue(
    queue: str, dead_letter: str
) -> None:
    # Uma DLQ compartilhada misturaria jobs de donos e limites diferentes, e o
    # reprocessamento manual teria que adivinhar de onde cada um veio.
    result = outcome(queue, 99)

    assert isinstance(result, DeadLetter)
    assert result.queue == dead_letter


def test_a_permanent_failure_does_not_wait_for_the_limit() -> None:
    # Repetir cinco vezes um payload inválido é queimar VT e alarme por nada; o
    # que o classificador sabe, o limite não precisa redescobrir.
    result = outcome(INBOUND, attempt=1, failure=Failure.PERMANENT)

    assert isinstance(result, DeadLetter)


def test_an_unknown_failure_retries_but_is_not_immortal() -> None:
    # A decisão registrada: desconhecido repete como transitório, e o limite é o
    # que impede "transitório para sempre".
    assert isinstance(outcome(INBOUND, 1, Failure.UNKNOWN), Retry)
    assert isinstance(outcome(INBOUND, 6, Failure.UNKNOWN), DeadLetter)


def test_the_retry_carries_the_backoff_of_its_attempt() -> None:
    result = outcome(INBOUND, attempt=2)

    assert isinstance(result, Retry)
    assert result.delay == timedelta(minutes=2)


def test_an_unknown_queue_is_a_programming_error() -> None:
    # Fila nova sem limite na tabela falha alto aqui, em vez de silenciosamente
    # herdar o limite de outra.
    with pytest.raises(KeyError):
        outcome("q_inventada", 1)

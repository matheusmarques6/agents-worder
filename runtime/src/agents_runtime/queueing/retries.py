"""Repetir com que espera, ou desistir para qual DLQ.

A tabela de limites é dirigida por dados de propósito: um `if fila == ...`
espalhado pelo consumidor é como uma quinta fila nasce sem limite nenhum.
"""

from dataclasses import dataclass
from datetime import timedelta

from agents_runtime.config import QueueingConfig
from agents_runtime.queueing.backoff import delay_for
from agents_runtime.queueing.failures import Failure
from agents_runtime.randomness import Randomness


@dataclass(frozen=True)
class Retry:
    """Devolve a mensagem à fila com `set_vt(delay)`."""

    delay: timedelta


@dataclass(frozen=True)
class DeadLetter:
    """Move para a DLQ da própria fila e arquiva a original."""

    queue: str


def decide(
    queue: str,
    *,
    attempt: int,
    failure: Failure,
    config: QueueingConfig,
    randomness: Randomness,
) -> Retry | DeadLetter:
    # Fila sem limite na tabela falha alto aqui, em vez de herdar em silêncio o
    # limite de outra.
    limit = config.retry_limits[queue]

    # O que o classificador já sabe, o limite não precisa redescobrir: repetir
    # cinco vezes um payload inválido é queimar VT e alarme por nada.
    if failure is Failure.PERMANENT or attempt > limit:
        return DeadLetter(queue=f"{queue}_dlq")

    return Retry(delay=delay_for(attempt, config=config, randomness=randomness))

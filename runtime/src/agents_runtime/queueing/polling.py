"""Weighted polling 8:4:2:1 e promoção por idade.

Prioridade estrita é proibida, e o motivo é concreto: um `order_paid` atrás de
uma fila de entrada movimentada chega tarde demais para cancelar o funil, e o
cliente que acabou de pagar recebe a cobrança. A proporção existe para que
nenhuma fila morra de fome.

A rotação é suave, não em blocos: oito turnos seguidos de entrada dariam a
proporção certa com a latência errada, porque as outras filas esperariam a
rajada inteira passar.
"""

from datetime import timedelta

from agents_runtime.config import QueueingConfig
from agents_runtime.queueing import DOMAIN_EVENTS, EVALS, INBOUND, SCHEDULED


def _schedule(weights: dict[str, int]) -> tuple[str, ...]:
    """Round-robin ponderado suave: cada fila espalhada pela janela inteira."""
    total = sum(weights.values())
    credit = dict.fromkeys(weights, 0.0)
    order: list[str] = []

    for _ in range(total):
        for queue, weight in weights.items():
            credit[queue] += weight
        chosen = max(credit, key=lambda queue: (credit[queue], weights[queue]))
        credit[chosen] -= total
        order.append(chosen)

    return tuple(order)


_DEFAULT_SCHEDULE = _schedule(QueueingConfig().weights)
WINDOW = len(_DEFAULT_SCHEDULE)


def next_queue(
    has_work: dict[str, bool], cursor: int, *, config: QueueingConfig
) -> tuple[str | None, int]:
    """A fila da vez e o cursor seguinte.

    Turno de fila vazia não se perde: é emprestado à fila mais prioritária com
    trabalho pendente (arquitetura §ADR-5). Sem nada a fazer, devolve None e o
    cursor intacto — quem decide dormir é o laço; a política não inventa
    trabalho.
    """
    schedule = (
        _DEFAULT_SCHEDULE
        if config.weights == QueueingConfig().weights
        else _schedule(config.weights)
    )

    slot = schedule[cursor % len(schedule)]
    if has_work.get(slot):
        return slot, cursor + 1

    candidates = [queue for queue, working in has_work.items() if working]
    if not candidates:
        return None, cursor

    return max(candidates, key=lambda queue: config.weights.get(queue, 0)), cursor + 1


# Um nível acima, na ordem da proporção. `q_evals` não sobe: avaliação é melhor
# esforço por definição, e promovê-la seria deixá-la competir com a conversa de
# um cliente.
_ONE_LEVEL_UP = {SCHEDULED: DOMAIN_EVENTS, DOMAIN_EVENTS: INBOUND}


def effective_queue(queue: str, age: timedelta, *, config: QueueingConfig) -> str:
    """A classe com que a mensagem deve ser tratada, dada a idade dela."""
    if queue == DOMAIN_EVENTS and age > config.promote_domain_after:
        return _ONE_LEVEL_UP[DOMAIN_EVENTS]
    if queue == SCHEDULED and age > config.promote_scheduled_after:
        return _ONE_LEVEL_UP[SCHEDULED]
    if queue == EVALS:
        return EVALS
    return queue

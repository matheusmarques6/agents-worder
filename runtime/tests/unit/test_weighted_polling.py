"""Unidade 4 — weighted polling 8:4:2:1 e promoção por idade.

Prioridade estrita é proibida pela arquitetura, e o motivo é concreto: um
evento de `order_paid` que fica atrás de uma fila de entrada movimentada chega
tarde demais para cancelar o funil, e o cliente que acabou de pagar recebe uma
cobrança. A proporção existe para que nenhuma fila morra de fome.

A promoção por idade é a segunda metade da mesma ideia: o que envelhece sobe,
porque o custo de um evento atrasado cresce com o atraso.
"""

from collections import Counter
from datetime import timedelta

import pytest

from agents_runtime.config import QueueingConfig
from agents_runtime.queueing import DOMAIN_EVENTS, EVALS, INBOUND, SCHEDULED
from agents_runtime.queueing.polling import WINDOW, effective_queue, next_queue

CONFIG = QueueingConfig()

ALL_BUSY = {INBOUND: True, DOMAIN_EVENTS: True, SCHEDULED: True, EVALS: True}


def poll(has_work: dict[str, bool], turns: int) -> list[str]:
    picked: list[str] = []
    cursor = 0
    for _ in range(turns):
        queue, cursor = next_queue(has_work, cursor, config=CONFIG)
        if queue is not None:
            picked.append(queue)
    return picked


def test_a_full_window_respects_the_proportion() -> None:
    # 8 + 4 + 2 + 1 = 15, que é o tamanho da janela por construção.
    assert WINDOW == 15

    assert Counter(poll(ALL_BUSY, WINDOW)) == {
        INBOUND: 8,
        DOMAIN_EVENTS: 4,
        SCHEDULED: 2,
        EVALS: 1,
    }


def test_no_queue_starves_even_when_the_busiest_never_empties() -> None:
    # A asserção que proíbe prioridade estrita. Com tudo cheio, as quatro filas
    # aparecem numa única janela — não "eventualmente", nesta.
    assert set(poll(ALL_BUSY, WINDOW)) == {INBOUND, DOMAIN_EVENTS, SCHEDULED, EVALS}


def test_the_busiest_queue_does_not_take_the_whole_window_in_one_block() -> None:
    # Oito turnos seguidos de entrada seriam a proporção certa com a latência
    # errada: as outras filas esperariam a rajada inteira passar.
    picked = poll(ALL_BUSY, WINDOW)
    longest_run = max(
        len(list(group)) for group in _runs(picked)
    )

    assert longest_run < 8


def _runs(items: list[str]):
    current: list[str] = []
    for item in items:
        if current and item != current[-1]:
            yield current
            current = []
        current.append(item)
    if current:
        yield current


def test_an_empty_queue_lends_its_turn_to_the_most_urgent_one_with_work() -> None:
    # "Slots ociosos são emprestados à fila mais prioritária com trabalho
    # pendente" — o turno não se perde, é o ponto todo de emprestar.
    without_inbound = {**ALL_BUSY, INBOUND: False}

    picked = Counter(poll(without_inbound, WINDOW))

    assert picked[INBOUND] == 0
    assert picked[DOMAIN_EVENTS] == 12
    assert picked[SCHEDULED] == 2
    assert picked[EVALS] == 1


def test_nothing_to_do_returns_nothing() -> None:
    # O laço decide dormir; a função de política não inventa trabalho.
    queue, cursor = next_queue(dict.fromkeys(ALL_BUSY, False), 0, config=CONFIG)

    assert queue is None
    assert cursor == 0


class TestPromotionByAge:
    def test_a_domain_event_older_than_two_minutes_is_treated_as_inbound(self) -> None:
        # É o caso do pagamento que precisa cancelar o funil antes do próximo
        # toque sair.
        assert effective_queue(DOMAIN_EVENTS, timedelta(minutes=3), config=CONFIG) == INBOUND

    def test_a_fresh_domain_event_stays_where_it_is(self) -> None:
        assert (
            effective_queue(DOMAIN_EVENTS, timedelta(seconds=30), config=CONFIG)
            == DOMAIN_EVENTS
        )

    def test_a_scheduled_job_older_than_ten_minutes_moves_up_one_level(self) -> None:
        # Um nível, não até o topo: um toque atrasado é urgente, não mais
        # urgente que a pessoa que está escrevendo agora.
        assert (
            effective_queue(SCHEDULED, timedelta(minutes=11), config=CONFIG) == DOMAIN_EVENTS
        )

    @pytest.mark.parametrize("age", [timedelta(minutes=1), timedelta(hours=5)])
    def test_evaluation_never_gets_promoted(self, age: timedelta) -> None:
        # Avaliação é melhor esforço por definição; promovê-la seria deixá-la
        # competir com a conversa de um cliente.
        assert effective_queue(EVALS, age, config=CONFIG) == EVALS

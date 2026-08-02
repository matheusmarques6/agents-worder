"""Unidade 4 — backoff exponencial com jitter.

A escada canônica é a da arquitetura §ADR-4: **30s, 2min, 8min…** — base 30s,
fator 4. Os números da escada são o CENTRO, e o jitter os espalha em volta;
fosse o jitter só para baixo, a escada documentada nunca seria observada.

A aleatoriedade entra como dependência, do mesmo jeito que o relógio. Sem isso
o teste vira loteria: passa oito vezes e reprova na nona, e ninguém consegue
dizer se o código mudou ou se foi o dado.
"""

from datetime import timedelta

import pytest

from agents_runtime.config import QueueingConfig
from agents_runtime.queueing.backoff import delay_for
from tests.support.randomness import FixedRandomness

CONFIG = QueueingConfig()

# O sorteio no centro devolve exatamente a escada da arquitetura.
CENTRED = FixedRandomness(position=0.5)
LOWEST = FixedRandomness(position=0.0)
HIGHEST = FixedRandomness(position=1.0)


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [(1, timedelta(seconds=30)), (2, timedelta(minutes=2)), (3, timedelta(minutes=8))],
)
def test_the_ladder_is_the_one_the_architecture_documents(
    attempt: int, expected: timedelta
) -> None:
    assert delay_for(attempt, config=CONFIG, randomness=CENTRED) == expected


def test_the_delay_only_grows() -> None:
    # Monotônico até o teto. Um backoff que encolhe é um retry que volta a
    # bater na mesma parede mais cedo.
    delays = [delay_for(attempt, config=CONFIG, randomness=CENTRED) for attempt in range(1, 8)]

    assert delays == sorted(delays)


def test_the_cap_holds() -> None:
    # O teto não morde nos limites atuais — cinco tentativas param em ~34 min —
    # e existe para o dia em que um limite subir sem ninguém reler esta conta.
    assert delay_for(99, config=CONFIG, randomness=HIGHEST) == CONFIG.backoff_cap


def test_the_jitter_stays_inside_its_band() -> None:
    # ±20% em volta da escada. O jitter existe para que mil consumidores que
    # falharam juntos não voltem juntos; se ele fosse largo demais, a escada
    # deixaria de significar alguma coisa.
    nominal = timedelta(seconds=30)

    assert delay_for(1, config=CONFIG, randomness=LOWEST) == nominal * 0.8
    assert delay_for(1, config=CONFIG, randomness=HIGHEST) == nominal * 1.2


def test_the_first_attempt_is_attempt_one() -> None:
    # Fronteira que erra fácil: `attempt=0` seria a tentativa original, que não
    # é um retry e não tem espera.
    with pytest.raises(ValueError):
        delay_for(0, config=CONFIG, randomness=CENTRED)

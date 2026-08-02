"""O acaso, como dependência.

Mesma razão do relógio (`agents_runtime.clock`): uma regra que sorteia sozinha
não é reprodutível, e um teste que depende de sorte reprova de vez em quando
sem que nada tenha mudado.

Este é o único módulo autorizado a tocar `random`;
`tests/unit/test_no_direct_randomness.py` reprova o build se aparecer um
segundo.
"""

import random
from typing import Protocol, runtime_checkable


@runtime_checkable
class Randomness(Protocol):
    """Sortear um número, como capacidade injetada."""

    def uniform(self, low: float, high: float) -> float:
        """Um valor entre `low` e `high`, inclusive nas pontas."""
        ...


class SystemRandomness:
    """O acaso de produção."""

    def uniform(self, low: float, high: float) -> float:
        return random.uniform(low, high)

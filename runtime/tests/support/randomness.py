"""O acaso congelado, para o nível `unit`.

Duplo de teste, e por isso mora em `tests/` e não no pacote do runtime — a
mesma regra do `FrozenClock` (decisão 8 do E0): duplo de teste não viaja na
imagem de produção.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FixedRandomness:
    """Sempre a mesma posição relativa dentro da faixa.

    `position=0.5` devolve o centro, que é onde a escada documentada aparece;
    0.0 e 1.0 são as bordas do jitter, que é o que os testes de limite querem.
    """

    position: float = 0.5

    def uniform(self, low: float, high: float) -> float:
        return low + (high - low) * self.position

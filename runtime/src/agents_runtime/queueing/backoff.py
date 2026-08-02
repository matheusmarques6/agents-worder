"""Backoff exponencial com jitter.

A escada é a da arquitetura §ADR-4 — 30s, 2min, 8min… — e os números dela são o
CENTRO da faixa, não o piso: o jitter espalha em volta. Fosse só para baixo, a
escada documentada nunca seria observada em produção e o documento passaria a
descrever outra coisa.
"""

from datetime import timedelta

from agents_runtime.config import QueueingConfig
from agents_runtime.randomness import Randomness


def delay_for(attempt: int, *, config: QueueingConfig, randomness: Randomness) -> timedelta:
    """Quanto esperar antes da tentativa `attempt` (a primeira retentativa é 1)."""
    if attempt < 1:
        raise ValueError(
            f"tentativa deve começar em 1 — {attempt} seria a execução original, que não espera"
        )

    # A escada é construída passo a passo e para no teto, em vez de calcular
    # `base * factor ** (attempt - 1)` e cortar depois: com um `attempt` alto o
    # expoente estoura antes de alguém ter a chance de aplicar o limite.
    capped = config.backoff_base
    for _ in range(attempt - 1):
        if capped >= config.backoff_cap:
            break
        capped *= config.backoff_factor
    capped = min(capped, config.backoff_cap)

    spread = randomness.uniform(1 - config.jitter_ratio, 1 + config.jitter_ratio)
    # O teto é teto mesmo com o jitter para cima: um backoff que ultrapassa o
    # limite acordado deixa de ser o limite acordado.
    return min(capped * spread, config.backoff_cap)

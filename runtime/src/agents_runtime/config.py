"""Os valores canônicos, num lugar só.

A tabela do `CLAUDE.md` é a fonte; isto é a transcrição dela em código, e a
composição (`app.py`) é quem lê. Um número dessa tabela escrito num call site é
um número que vai divergir do documento na primeira vez que alguém mudar um dos
dois — e ninguém vai notar, porque os dois "funcionam".

Tudo é `frozen` e recebido por parâmetro em vez de importado onde se usa: é o
que deixa a suíte de `pipeline` rodar com um debounce de 20ms em vez de esperar
dez segundos de verdade, sem que a regra saiba que está sendo testada.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from agents_runtime.queueing import DOMAIN_EVENTS, EVALS, INBOUND, SCHEDULED


def _weights() -> dict[str, int]:
    """8 : 4 : 2 : 1 — a proporção do weighted polling (arquitetura §ADR-5)."""
    return {INBOUND: 8, DOMAIN_EVENTS: 4, SCHEDULED: 2, EVALS: 1}


def _retry_limits() -> dict[str, int]:
    """Quantas vezes cada fila insiste antes da DLQ."""
    return {INBOUND: 5, DOMAIN_EVENTS: 5, SCHEDULED: 3, EVALS: 2}


@dataclass(frozen=True)
class QueueingConfig:
    """Filas, esperas e prazos."""

    # Visibilidade e sinal de vida: o heartbeat renova antes do VT vencer, com
    # folga de 15s para uma rede ruim não custar uma reentrega.
    visibility_timeout: timedelta = timedelta(seconds=60)
    heartbeat_every: timedelta = timedelta(seconds=45)

    # Debounce da entrada e o tique do coalescer.
    inbound_debounce: timedelta = timedelta(seconds=10)
    coalescer_tick: timedelta = timedelta(seconds=2)

    # A escada da arquitetura: 30s, 2min, 8min… O teto não morde nos limites
    # atuais (cinco tentativas param em ~34 min); existe para o dia em que um
    # limite subir sem ninguém reler esta conta.
    backoff_base: timedelta = timedelta(seconds=30)
    backoff_factor: int = 4
    backoff_cap: timedelta = timedelta(hours=1)
    # ±20% em volta da escada. Espalha o rebanho que falhou junto sem que os
    # números documentados deixem de ser o que se observa.
    jitter_ratio: float = 0.2

    weights: dict[str, int] = field(default_factory=_weights)
    retry_limits: dict[str, int] = field(default_factory=_retry_limits)

    # Promoção por idade: o custo de um evento atrasado cresce com o atraso.
    promote_domain_after: timedelta = timedelta(minutes=2)
    promote_scheduled_after: timedelta = timedelta(minutes=10)

    # Válido SÓ enquanto o runtime for um processo asyncio único (ADR-2). Ir a
    # multi-processo exige migrar isto para uma lease distribuída antes.
    tenant_concurrency: int = 3

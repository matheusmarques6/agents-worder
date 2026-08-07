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

    # A lease da conversa (ADR-6): renovável pelo heartbeat acima enquanto a
    # FASE 2 durar. Vencida, é lease livre — é assim que o trabalho de um
    # processo morto volta a ser feito.
    conversation_lease: timedelta = timedelta(minutes=2)

    # Debounce da entrada e o tique do coalescer.
    inbound_debounce: timedelta = timedelta(seconds=10)
    coalescer_tick: timedelta = timedelta(seconds=2)

    # A varredura dos toques vencidos (dicionário §5.5). Um minuto porque a
    # cadência de um funil se mede em horas: a latência do tique é ruído ao lado
    # do intervalo entre dois toques, e varrer mais rápido só multiplicaria uma
    # consulta cross-tenant sem mudar o que o contato vê.
    dispatcher_tick: timedelta = timedelta(minutes=1)

    # A varredura do silêncio (RF-033b): três funis ignorados viram uma linha em
    # `suppression_list`. Quinze minutos porque nada nela é sensível a latência
    # — entre dois funis distintos existe o cooldown de 72h, então o contato não
    # receberia nada nesse intervalo de qualquer forma, e a escada continua
    # protegendo enquanto a linha não existe. O que a varredura acrescenta é o
    # FATO gravado, não a proteção.
    silence_sweep_tick: timedelta = timedelta(minutes=15)

    # A reconciliação por poll (ADR-3, E3 S8). DOIS números, e a distinção
    # importa: `reconcile_stale_after` é a PROMESSA — nenhuma loja fica mais de
    # quinze minutos sem alguém perguntar a ela — e `reconcile_tick` é a
    # frequência com que a varredura verifica essa promessa. Iguais, a promessa
    # viraria trinta minutos: um tique que chega um segundo antes de a loja
    # vencer não a pega, e a próxima chance é um tique inteiro depois. É a mesma
    # leitura de `dispatcher_tick` contra `due_at`.
    reconcile_tick: timedelta = timedelta(minutes=5)
    reconcile_stale_after: timedelta = timedelta(minutes=15)

    # A varredura de saúde (E3 S11): as três falhas silenciosas do marco viram
    # linha em `public.alerts`. Cinco minutos porque nenhuma delas é urgente no
    # sentido de segundos — todas já aconteceram quando a varredura passa, e o
    # que a varredura muda é quanto tempo alguém leva para SABER.
    health_sweep_tick: timedelta = timedelta(minutes=5)

    # Quanto tempo um toque pode ficar `enqueued` antes de ser dado como preso.
    # DECISÃO, e derivada: a `q_scheduled` tenta 3 vezes com a escada de backoff
    # (30s, 2min, 8min), então um job vivo termina — ou vai para a DLQ — em
    # ~10 minutos. Trinta é o triplo disso: passado esse prazo o toque não está
    # esperando, está perdido.
    touch_stuck_after: timedelta = timedelta(minutes=30)

    # Há quanto tempo uma loja precisa vir falhando para virar alerta. O plano
    # pede "mais de N tiques"; com `reconcile_tick` de 5 minutos, uma hora são
    # doze passes falhados seguidos — muito além do soluço de plataforma que o
    # cinto de segurança do ADR-3 absorve sozinho por repetição.
    sync_error_after: timedelta = timedelta(hours=1)

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

    # --- the composition's own rhythms (E1 · PR 2a) -------------------------
    # A conversation someone else holds is retried shortly — 'shortly' because
    # the other worker usually finishes within its lease, not within ours.
    busy_retry: timedelta = timedelta(seconds=2)
    # How long a poll loop rests when every queue it serves is empty.
    idle_pause: timedelta = timedelta(seconds=1)
    sender_poll: timedelta = timedelta(seconds=1)
    # The milestone proof is 'heartbeat ≤ 3 min'; beating every 30s leaves five
    # missed beats of slack before the alert would fire.
    process_heartbeat_every: timedelta = timedelta(seconds=30)

    # The outbox claim lease. Expired mid-'sending' means the sender died with
    # the outcome unknown — the sweep turns that into state, never a resend.
    send_lease: timedelta = timedelta(seconds=60)
    # How long an unknown may wait for correlation evidence before a human is
    # asked. DECISION, not canon: 5 minutes chosen here (status webhooks land
    # in seconds); the canonical table should absorb or veto it (pendência).
    unknown_review_after: timedelta = timedelta(minutes=5)

    # How long a row waits when the number's own ceiling — not its jitter — held
    # it back (S7). The jitter knows exactly what is left of itself and says so;
    # a daily cap has no such answer, because "tomorrow" depends on a clock the
    # rule deliberately does not read. Fifteen minutes is a recheck interval, not
    # a canonical number: it costs one cheap claim and it means a warm-up stage
    # advanced by an operator takes effect within the quarter hour instead of at
    # midnight. DECISION, recorded here rather than in the CLAUDE.md table,
    # because nothing in the product depends on the value.
    paced_retry: timedelta = timedelta(minutes=15)


def config_from_env(environ: "dict[str, str]") -> QueueingConfig:
    """The canonical defaults, overridable per environment.

    This exists for exactly one consumer: the pipeline suite, which runs the
    real process with a 50ms coalescer tick instead of waiting two real
    seconds per tick. Production sets none of these and gets the CLAUDE.md
    table verbatim.
    """

    def _ms(name: str, fallback: timedelta) -> timedelta:
        raw = environ.get(name)
        return timedelta(milliseconds=int(raw)) if raw else fallback

    base = QueueingConfig()
    return QueueingConfig(
        visibility_timeout=_ms("AGENTS_VT_MS", base.visibility_timeout),
        heartbeat_every=_ms("AGENTS_WORK_HEARTBEAT_MS", base.heartbeat_every),
        conversation_lease=_ms("AGENTS_LEASE_MS", base.conversation_lease),
        coalescer_tick=_ms("AGENTS_COALESCER_TICK_MS", base.coalescer_tick),
        dispatcher_tick=_ms("AGENTS_DISPATCHER_TICK_MS", base.dispatcher_tick),
        silence_sweep_tick=_ms("AGENTS_SILENCE_TICK_MS", base.silence_sweep_tick),
        reconcile_tick=_ms("AGENTS_RECONCILE_TICK_MS", base.reconcile_tick),
        reconcile_stale_after=_ms("AGENTS_RECONCILE_STALE_MS", base.reconcile_stale_after),
        health_sweep_tick=_ms("AGENTS_HEALTH_TICK_MS", base.health_sweep_tick),
        touch_stuck_after=_ms("AGENTS_TOUCH_STUCK_MS", base.touch_stuck_after),
        sync_error_after=_ms("AGENTS_SYNC_ERROR_MS", base.sync_error_after),
        busy_retry=_ms("AGENTS_BUSY_RETRY_MS", base.busy_retry),
        idle_pause=_ms("AGENTS_IDLE_PAUSE_MS", base.idle_pause),
        sender_poll=_ms("AGENTS_SENDER_POLL_MS", base.sender_poll),
        process_heartbeat_every=_ms(
            "AGENTS_PROCESS_HEARTBEAT_MS", base.process_heartbeat_every
        ),
        send_lease=_ms("AGENTS_SEND_LEASE_MS", base.send_lease),
        unknown_review_after=_ms("AGENTS_REVIEW_MS", base.unknown_review_after),
        backoff_base=_ms("AGENTS_BACKOFF_BASE_MS", base.backoff_base),
        backoff_cap=_ms("AGENTS_BACKOFF_CAP_MS", base.backoff_cap),
    )

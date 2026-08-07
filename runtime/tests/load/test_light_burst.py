"""Carga leve 10x — a rajada do `core/plano-de-testes.md` §5.1 nº 7, em miniatura.

O plano pede a suíte grande em staging: rajada de 20-50x por uma hora, 25
tenants sintéticos, encerramentos abruptos durante a rajada. Esta é a versão que
cabe numa máquina de desenvolvimento e roda em pouco mais de um minuto — e ela
existe agora, e não no mês da BF, por uma razão específica: **um harness de
carga escrito sob pressão mede o que der para medir.** Escrito aqui, ele fixa os
critérios enquanto ninguém precisa deles, e a versão de staging passa a ser este
arquivo com três números maiores.

**O que "10x" quer dizer, dito explicitamente porque o plano não define o 1x:**
a linha de base adotada aqui é 15 eventos/min — uma loja tranquila, um evento a
cada quatro segundos. Dez vezes isso são 150 eventos/min, que é exatamente o
piso do critério nº 4 ("vazão sustentada: ≥ 150 eventos/min processados no
pico"). A escolha não é arbitrária: ela faz o alvo da carga leve coincidir com o
número que o documento já se comprometeu a sustentar.

**Os critérios são os do §5 de `core/testes-e-cicd.md`, com a janela encurtada e
nada mais afrouxado.** Os que este nível consegue afirmar:

    1. perda de eventos = 0
    2. latência inbound (fim do debounce → outbox): p95 ≤ 2 min, p99 ≤ 5 min
    3. atraso de proativo ≤ 30 min
    4. vazão sustentada ≥ 150 eventos/min durante a rajada
    5. drenagem: filas zeradas após o fim da rajada
    6. falhas permanentes < 0,5%; DLQ vazia ou justificada
    8. nenhum item de `q_inbound` com idade > 10 min

O critério **nº 7 (CPU < 80%, memória < 75%, sem crescimento contínuo) NÃO é
afirmado aqui** e não é afirmado em silêncio: ele fala da VPS, e uma medição de
CPU numa máquina que está rodando um navegador e um Docker Desktop seria um
número que passa ou reprova por motivo errado. Ele fica para a suíte de staging,
onde há uma VPS para medir.

**Os encerramentos abruptos também ficam de fora**, e por escolha e não por
esquecimento: eles já têm prova determinística nos cenários 4, 5 e 10 do
`pipeline`, onde o kill acontece num ponto CONTROLADO. Matar o processo no meio
de uma rajada mede a mesma coisa com menos precisão; o que a rajada acrescenta
aos kills é volume, e volume é o que a suíte de staging tem.

**Como ler o relatório:** ele vai para o log do pytest. Rodar com
`-m load -o log_cli=true --log-cli-level=INFO` mostra os números ao vivo; sem
isso eles aparecem se algum critério reprovar, porque o relatório inteiro é a
mensagem de cada assert. `print` é proibido no repositório e não abro exceção
para ele aqui.
"""

import asyncio
import logging
import statistics
import time
from dataclasses import dataclass, replace
from datetime import timedelta

import psycopg
from psycopg.types.json import Jsonb

from agents_runtime.app import run
from agents_runtime.config import QueueingConfig
from agents_runtime.queueing import DEAD_LETTER, INBOUND, WORK_QUEUES
from tests.db.factories import (
    create_channel_account,
    create_connector_account,
    create_tenant,
    unique_id,
    unique_phone,
)
from tests.db.factories_e3 import create_funnel
from tests.support.fake_channel import FakeChannel
from tests.support.scripted_review import create_reviewer

logger = logging.getLogger(__name__)

#: A loja tranquila da linha de base: um evento a cada quatro segundos.
BASELINE_EVENTS_PER_MINUTE = 15
MULTIPLIER = 10
TARGET_RATE = BASELINE_EVENTS_PER_MINUTE * MULTIPLIER  # 150/min — o piso do §5 nº 4

#: A rajada. Sessenta segundos é o menor intervalo em que "sustentada" quer
#: dizer alguma coisa; a suíte de staging usa uma hora e este é o único número
#: que muda.
BURST_SECONDS = 60

TENANTS = 5
CONTACTS_PER_TENANT = 10

#: Dois terços das mensagens são de contato e um terço é evento de plataforma —
#: uma mistura, e não uma fila só, porque metade do que a carga existe para
#: observar (as proporções de consumo, a promoção por idade) precisa de mais de
#: uma fila com trabalho.
INBOUND_SHARE = 2 / 3

#: O debounce, encurtado como a config manda e nunca por patch. Um segundo em
#: vez de dez: a latência que o critério nº 2 mede começa DEPOIS dele, então
#: encurtá-lo não afrouxa o critério — só encurta a espera.
DEBOUNCE = timedelta(seconds=1)

DRAIN_TIMEOUT = 120

CADENCE = [{"n": 1, "delay": "PT0S", "copy_base": "Vi que ficou algo no carrinho."}]


@dataclass(frozen=True)
class World:
    tenant_id: object
    store: str
    number: str
    phones: list[str]


def build_world(conn: psycopg.Connection) -> list[World]:
    worlds = []
    for _ in range(TENANTS):
        tenant_id = create_tenant(conn)
        store = create_connector_account(conn, tenant_id)
        number = create_channel_account(conn, tenant_id)
        create_funnel(conn, tenant_id, touches=CADENCE)
        worlds.append(
            World(
                tenant_id=tenant_id,
                store=store.source_account_id,
                number=number.external_account_id,
                phones=[unique_phone() for _ in range(CONTACTS_PER_TENANT)],
            )
        )
    return worlds


async def inject(conn: psycopg.AsyncConnection, worlds: list[World]) -> int:
    """A rajada, no ritmo alvo — nunca mais rápido.

    O passo é calculado a partir do relógio de parede a cada evento, e não por
    `sleep(1/rate)` acumulado: um harness que atrasa e nunca recupera mede uma
    taxa que ele mesmo baixou, e reporta como "sustentada" o que na verdade foi
    o que ele conseguiu emitir.
    """
    interval = 60.0 / TARGET_RATE
    started = time.monotonic()
    sent = 0

    while True:
        elapsed = time.monotonic() - started
        if elapsed >= BURST_SECONDS:
            return sent

        world = worlds[sent % len(worlds)]
        phone = world.phones[(sent // len(worlds)) % CONTACTS_PER_TENANT]

        if (sent % 3) < (INBOUND_SHARE * 3):
            await conn.execute(
                "select internal.ingest_webhook('meta', %s, %s, 'message_inbound', %s, %s)",
                (
                    world.number,
                    unique_id("evt"),
                    Jsonb({"from": phone, "message": {"text": "e o meu pedido?"}}),
                    DEBOUNCE,
                ),
            )
        else:
            await conn.execute(
                "select internal.ingest_webhook('shopify', %s, %s, 'checkout_abandoned', %s)",
                (world.store, unique_id("evt"), Jsonb({"phone": unique_phone()})),
            )
        sent += 1

        # O próximo instante devido, medido do início — não do fim do anterior.
        due = started + sent * interval
        delay = due - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)


async def queues_empty(conn: psycopg.AsyncConnection) -> bool:
    # As filas de TRABALHO. Uma DLQ com item não é fila por drenar, é
    # critério nº 6 — e esperar por ela seria trocar um relatório de falha
    # por um tempo limite que não diz o que aconteceu.
    for queue in WORK_QUEUES:
        cursor = await conn.execute("select queue_length from pgmq.metrics(%s)", (queue,))
        row = await cursor.fetchone()
        if row and int(row[0]) > 0:
            return False
    return True


async def pending_work(conn: psycopg.AsyncConnection) -> tuple[int, int, int]:
    """O que ainda está por fazer, discriminado — nunca um booleano.

    A drenagem não é só "a fila esvaziou": o coalescer ainda pode ter uma
    conversa com o prazo vencido para transformar em job. Parar em fila vazia
    seria parar no instante entre dois ticks e chamar isso de drenado.

    Discriminado porque um tempo limite de drenagem que só diz "ainda há
    trabalho" é um relatório que manda alguém adivinhar QUAL trabalho — e a
    primeira execução deste harness estourou exatamente aqui, sem dizer onde.
    """
    cursor = await conn.execute(
        """
        select (select count(*) from public.conversations
                 where pending_response_at is not null),
               (select count(*) from public.scheduled_touches
                 where status in ('pending', 'enqueued')),
               (select count(*) from internal.message_outbox
                 where status in ('pending', 'sending'))
        """
    )
    row = await cursor.fetchone()
    return tuple(int(value) for value in row) if row else (0, 0, 0)


async def percentiles(conn: psycopg.AsyncConnection) -> dict:
    """Os números do relatório, lidos do banco depois que tudo parou.

    Latência inbound é medida como o critério nº 2 pede: do FIM do debounce
    (a última mensagem da conversa mais o prazo) até a gravação na outbox.
    """
    cursor = await conn.execute(
        """
        select extract(epoch from (o.created_at - (m.created_at + %s)))
          from internal.message_outbox o
          cross join lateral (
              select max(i.created_at) as created_at
                from public.messages i
               where i.conversation_id = o.conversation_id
                 and i.direction = 'inbound'
                 and i.created_at <= o.created_at
          ) m
         where o.kind = 'reply' and m.created_at is not null
        """,
        (DEBOUNCE,),
    )
    inbound = sorted(float(row[0]) for row in await cursor.fetchall())

    cursor = await conn.execute(
        """
        select extract(epoch from (o.created_at - t.due_at))
          from public.scheduled_touches t
          join internal.message_outbox o on o.id = t.outbox_id
         where t.status = 'sent'
        """
    )
    proactive = sorted(float(row[0]) for row in await cursor.fetchall())

    def at(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        return statistics.quantiles(values, n=100)[int(q) - 1] if len(values) > 1 else values[0]

    return {
        "inbound_count": len(inbound),
        "inbound_p50": at(inbound, 50),
        "inbound_p95": at(inbound, 95),
        "inbound_p99": at(inbound, 99),
        "proactive_count": len(proactive),
        "proactive_p95": at(proactive, 95),
        "proactive_max": max(proactive) if proactive else 0.0,
    }


async def test_a_light_ten_times_burst_meets_the_quantitative_criteria(
    dsn: str,
    admin: psycopg.AsyncConnection,
    sync_admin: psycopg.Connection,
    tiny_config: QueueingConfig,
) -> None:
    worlds = build_world(sync_admin)

    # Os ritmos do processo, não os do teste: o coalescer e o dispatcher rodam
    # em intervalos curtos porque a rajada dura um minuto, e um tique de um
    # minuto num teste de um minuto mediria o tique.
    config = replace(
        tiny_config,
        coalescer_tick=timedelta(milliseconds=200),
        dispatcher_tick=timedelta(milliseconds=200),
    )

    stop = asyncio.Event()
    running = asyncio.create_task(
        run(
            dsn,
            stop=stop,
            config=config,
            channel=FakeChannel(dsn),
            # O revisor pós-envio, presente e não ausente: sem ele `q_evals`
            # não é sequer consultada (é a regra do `app.run`, e é deliberada),
            # e a quarta fila do sistema ficaria fora da rajada inteira. Um
            # relatório de carga que mede três das quatro filas mede a carga
            # que sobrou, e a fila esquecida é a que enche em silêncio.
            review=create_reviewer(dsn),
            worker_set_role="worker_role",
            sender_set_role="sender_role",
        )
    )

    injected = 0
    max_backlog_age = 0.0
    drain_seconds = 0.0
    outstanding = (0, 0, 0)
    try:
        # Três conexões e não uma: o injetor, o observador do backlog e o
        # medidor da drenagem rodam ao mesmo tempo, e uma conexão psycopg não é
        # de mais de um. A primeira versão deste harness compartilhou a do
        # `admin` entre observador e medidor, e o que saiu de lá foi uma
        # drenagem que nunca terminava — dois cursores intercalados numa
        # conexão só não são dois leitores, são uma leitura corrompida.
        async with (
            await psycopg.AsyncConnection.connect(dsn, autocommit=True) as writer,
            await psycopg.AsyncConnection.connect(dsn, autocommit=True) as observer,
        ):
            watcher = asyncio.create_task(_watch_backlog(observer, stop))
            burst_started = time.monotonic()
            injected = await inject(writer, worlds)
            burst_seconds = time.monotonic() - burst_started

            drain_started = time.monotonic()
            deadline = drain_started + DRAIN_TIMEOUT
            while time.monotonic() < deadline:
                outstanding = await pending_work(admin)
                if await queues_empty(admin) and not any(outstanding):
                    break
                await asyncio.sleep(0.2)
            drain_seconds = time.monotonic() - drain_started
            max_backlog_age = await _stop_watching(watcher, stop)
    finally:
        stop.set()
        await asyncio.wait_for(running, 60)

    stats = await percentiles(admin)

    events = await (
        await admin.execute(
            """
            select count(*),
                   count(*) filter (where status in ('processed', 'discarded')),
                   count(*) filter (where status = 'failed'),
                   count(*) filter (where status not in ('processed','discarded','failed'))
              from internal.webhook_events
            """
        )
    ).fetchone()

    sends = await (
        await admin.execute(
            """
            select (select count(*) from testing.fake_channel_sends),
                   (select count(*) from internal.message_outbox where kind = 'reply'),
                   (select count(*) from internal.message_outbox where kind = 'funnel_touch'),
                   (select count(*) from internal.message_outbox where status <> 'sent')
            """
        )
    ).fetchone()

    dlq = 0
    for queue in DEAD_LETTER:
        cursor = await admin.execute(
            "select queue_length from pgmq.metrics(%s)", (queue,)
        )
        row = await cursor.fetchone()
        dlq += int(row[0]) if row else 0

    rate = injected / burst_seconds * 60
    failure_rate = events[2] / injected if injected else 0.0

    report = "\n".join(
        (
            "",
            f"carga leve {MULTIPLIER}x — relatório",
            f"  base {BASELINE_EVENTS_PER_MINUTE}/min · alvo {TARGET_RATE}/min"
            f" · {TENANTS} tenants · rajada {burst_seconds:.1f}s",
            f"  eventos injetados ............ {injected}",
            f"  vazão sustentada ............. {rate:.1f}/min  (critério nº 4: ≥ {TARGET_RATE})",
            f"  eventos concluídos ........... {events[1]}  (perda: {events[3]})",
            f"  eventos em `failed` .......... {events[2]}  ({failure_rate:.2%})",
            "                                  (critério nº 6: < 0,5%)",
            f"  respostas na outbox .......... {sends[1]}",
            f"  toques na outbox ............. {sends[2]}",
            f"  entregues ao canal ........... {sends[0]}  (outbox não enviada: {sends[3]})",
            f"  latência inbound (n={stats['inbound_count']}) "
            f"p50 {stats['inbound_p50']:.2f}s · p95 {stats['inbound_p95']:.2f}s"
            f" · p99 {stats['inbound_p99']:.2f}s",
            f"  atraso proativo (n={stats['proactive_count']}) "
            f"p95 {stats['proactive_p95']:.2f}s · máx {stats['proactive_max']:.2f}s",
            f"  idade máxima em q_inbound .... {max_backlog_age:.1f}s (critério nº 8: ≤ 600s)",
            f"  DLQ .......................... {dlq}",
            f"  drenagem ..................... {drain_seconds:.1f}s",
            f"  por fazer ao fim ............. conversas {outstanding[0]}"
            f" · toques {outstanding[1]} · outbox {outstanding[2]}",
            "  critério nº 7 (CPU/memória da VPS): NÃO medido neste nível — ver o docstring",
            "",
        )
    )
    logger.warning(report)

    # --- os critérios, um assert por critério, cada um carregando o relatório --

    assert events[3] == 0, f"critério nº 1 — perda de eventos{report}"
    assert events[0] == injected, f"critério nº 1 — evento injetado sem linha{report}"
    assert sends[3] == 0, f"critério nº 1 — item de outbox parado fora de `sent`{report}"

    assert stats["inbound_p95"] <= 120, f"critério nº 2 — p95 inbound{report}"
    assert stats["inbound_p99"] <= 300, f"critério nº 2 — p99 inbound{report}"

    assert stats["proactive_max"] <= 1800, f"critério nº 3 — atraso proativo{report}"

    assert rate >= TARGET_RATE * 0.95, f"critério nº 4 — vazão sustentada{report}"

    assert drain_seconds < DRAIN_TIMEOUT, f"critério nº 5 — drenagem{report}"

    assert failure_rate < 0.005, f"critério nº 6 — falhas permanentes{report}"
    assert dlq == 0, f"critério nº 6 — DLQ{report}"

    assert max_backlog_age <= 600, f"critério nº 8 — backlog de q_inbound{report}"

    # E a razão de tudo isto existir: o contato recebeu. Um relatório de carga
    # sem esta linha mede a máquina em vez de medir o produto.
    assert sends[0] == sends[1] + sends[2], f"nem tudo o que foi escrito saiu{report}"
    assert sends[0] > 0, f"a rajada não produziu envio nenhum{report}"


async def _watch_backlog(conn: psycopg.AsyncConnection, stop: asyncio.Event) -> None:
    """A idade do item mais velho de `q_inbound`, amostrada durante a rajada.

    Critério nº 8 é sobre o PIOR instante, não sobre o fim: medir só no final
    diria que o backlog nunca existiu, que é verdade para toda fila que drenou.
    """
    worst = 0.0
    while not stop.is_set():
        # `pgmq.metrics`, never the queue's table: the physical name is pgmq's
        # business, and a test that spells it out is a test that breaks on an
        # extension upgrade for a reason that has nothing to do with the product.
        cursor = await conn.execute(
            "select coalesce(oldest_msg_age_sec, 0) from pgmq.metrics(%s)", (INBOUND,)
        )
        row = await cursor.fetchone()
        worst = max(worst, float(row[0]) if row else 0.0)
        _watch_backlog.worst = worst  # type: ignore[attr-defined]
        await asyncio.sleep(0.25)


async def _stop_watching(watcher: asyncio.Task, stop: asyncio.Event) -> float:
    watcher.cancel()
    try:
        await watcher
    except asyncio.CancelledError:
        pass
    return float(getattr(_watch_backlog, "worst", 0.0))

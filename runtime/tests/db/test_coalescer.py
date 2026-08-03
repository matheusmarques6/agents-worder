"""A4 — o coalescer, e a transação única que faz rajada virar um job.

A ingestão marca o prazo; isto transforma prazo em job. O estado que não pode
existir é "perdeu o prazo sem ganhar job": a conversa fica com
`pending_response_at` limpo, nenhum job na fila, e a mensagem do cliente espera
para sempre sem que nada apareça em lugar nenhum.

O prazo é decidido em SQL contra `now()`, então as fábricas empurram
`pending_response_at` para o passado — a suíte não espera dez segundos de
verdade.
"""

import threading
import uuid

import psycopg
import pytest

from tests.db.conftest import TwoTenants, as_app_role
from tests.db.factories import Thread, create_thread, make_due

QUEUE = "q_inbound"


def coalesce(
    conn: psycopg.Connection, *, queue: str = QUEUE, limit: int = 100
) -> list[tuple]:
    return conn.execute(
        "select * from internal.coalesce_due_conversations(%s, %s)", (queue, limit)
    ).fetchall()


def queue_length(conn: psycopg.Connection, queue: str = QUEUE) -> int:
    return int(
        conn.execute("select queue_length from pgmq.metrics(%s)", (queue,)).fetchone()[0]
    )


def conversation_state(conn: psycopg.Connection, conversation_id: uuid.UUID) -> tuple:
    return conn.execute(
        """
        select processing_generation, pending_response_at is null
          from public.conversations where id = %s
        """,
        (conversation_id,),
    ).fetchone()


@pytest.fixture(autouse=True)
def empty_inbound(admin: psycopg.Connection):
    admin.execute("select pgmq.purge_queue(%s)", (QUEUE,))
    yield
    admin.execute("select pgmq.purge_queue(%s)", (QUEUE,))


@pytest.fixture
def due_thread(admin: psycopg.Connection, two_tenants: TwoTenants) -> Thread:
    thread = create_thread(admin, two_tenants.a.id)
    make_due(admin, thread.conversation_id, last_inbound_seq=3)
    return thread


# --- 1 · reversão -----------------------------------------------------------


def test_a_failure_halfway_through_leaves_the_deadline_untouched(
    admin: psycopg.Connection, due_thread: Thread
) -> None:
    """A asserção que a transação única existe para sustentar.

    Se o envio falhar, o bump da geração e a limpeza do prazo têm de voltar
    junto. Sobrar o prazo limpo sem job é a conversa que ninguém responde —
    e o próximo tique nem tentaria de novo, porque o prazo já teria sumido.
    """
    # A injeção de falha é uma fila que não existe: o pgmq tenta inserir numa
    # tabela ausente e a exceção sobe do meio da função, que é exatamente onde
    # ela precisa subir para este teste significar alguma coisa.
    with pytest.raises(psycopg.errors.UndefinedTable):
        coalesce(admin, queue="q_que_nao_existe")

    admin.rollback()

    generation, deadline_cleared = conversation_state(admin, due_thread.conversation_id)

    assert generation == 0
    assert deadline_cleared is False
    assert queue_length(admin) == 0


# --- 2 · sucesso ------------------------------------------------------------


def test_a_due_conversation_becomes_exactly_one_job(
    admin: psycopg.Connection, two_tenants: TwoTenants, due_thread: Thread
) -> None:
    jobs = coalesce(admin)

    # O tenant viaja no job desde a A3; o slot otel existe desde o PR 2a e
    # fica vazio até a T4. Cada campo entrou mudando este teste — é assim que
    # mudança de contrato deve aparecer.
    assert jobs == [(due_thread.conversation_id, 1, 3, two_tenants.a.id, None)]

    generation, deadline_cleared = conversation_state(admin, due_thread.conversation_id)
    assert (generation, deadline_cleared) == (1, True)

    assert queue_length(admin) == 1
    payload = admin.execute("select message from pgmq.read(%s, 5, 1)", (QUEUE,)).fetchone()[0]
    assert payload == {
        "conversation_id": str(due_thread.conversation_id),
        "generation": 1,
        "target_seq": 3,
        "tenant_id": str(two_tenants.a.id),
        "otel": None,
    }


# --- 3 · o prazo empurrado --------------------------------------------------


def test_a_conversation_whose_deadline_was_pushed_is_left_alone(
    admin: psycopg.Connection, two_tenants: TwoTenants
) -> None:
    # Empurrar o prazo é o que a ingestão faz a cada mensagem nova. O que se
    # prova aqui é a outra metade: empurrar ADIA — a rajada inteira cabe num
    # job só porque a última mensagem move a linha de chegada.
    thread = create_thread(admin, two_tenants.a.id)
    make_due(admin, thread.conversation_id, overdue_seconds=-30)  # vence daqui a 30s

    assert coalesce(admin) == []
    assert conversation_state(admin, thread.conversation_id) == (0, False)
    assert queue_length(admin) == 0


# --- 4 · dois coalescers ----------------------------------------------------


def test_a_second_coalescer_does_not_wait_for_the_first(
    dsn: str, admin: psycopg.Connection, due_thread: Thread
) -> None:
    """O que o `SKIP LOCKED` compra de verdade.

    Não é evitar job duplicado: em READ COMMITTED o segundo reavaliaria a linha
    depois do commit do primeiro e a veria fora do WHERE de qualquer jeito. É
    não ESPERAR. Sem `SKIP LOCKED`, dois tiques concorrentes viram fila de
    bloqueio e o tique de 2s deixa de ser 2s.

    Por isso a asserção é feita com a transação do primeiro ainda ABERTA, e com
    `lock_timeout` para que o bloqueio apareça como exceção em vez de pendurar
    a suíte.
    """
    first = psycopg.connect(dsn)
    second = psycopg.connect(dsn)
    try:
        first.execute("select 1")  # abre a transação
        assert len(coalesce(first)) == 1  # segura o lock, sem comitar

        second.execute("set lock_timeout = '2s'")
        assert coalesce(second) == []  # devolve na hora, não espera

        first.commit()
        second.commit()
    finally:
        first.close()
        second.close()

    assert queue_length(admin) == 1


def test_two_concurrent_coalescers_produce_one_job(
    dsn: str, admin: psycopg.Connection, due_thread: Thread
) -> None:
    barrier = threading.Barrier(2)
    collected: list[int] = []
    lock = threading.Lock()

    def tick() -> None:
        with psycopg.connect(dsn) as conn:
            barrier.wait(timeout=5)
            jobs = coalesce(conn)
            conn.commit()
        with lock:
            collected.append(len(jobs))

    workers = [threading.Thread(target=tick) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)

    assert sorted(collected) == [0, 1]
    assert queue_length(admin) == 1


# --- 5 · a segunda rodada ---------------------------------------------------


def test_a_new_message_produces_a_second_generation_and_a_second_job(
    admin: psycopg.Connection, due_thread: Thread
) -> None:
    # Duas gerações, dois jobs, nunca o mesmo. A geração é o que permite ao
    # worker descartar um job obsoleto sem precisar perguntar a ninguém.
    first = coalesce(admin)

    make_due(admin, due_thread.conversation_id, last_inbound_seq=5)
    second = coalesce(admin)

    assert [job[1] for job in first + second] == [1, 2]
    assert [job[2] for job in first + second] == [3, 5]
    assert queue_length(admin) == 2


# --- 6 · o limite -----------------------------------------------------------


def test_what_does_not_fit_stays_due_for_the_next_tick(
    admin: psycopg.Connection, two_tenants: TwoTenants
) -> None:
    threads = [create_thread(admin, two_tenants.a.id) for _ in range(3)]
    for thread in threads:
        make_due(admin, thread.conversation_id)

    jobs = coalesce(admin, limit=2)

    assert len(jobs) == 2
    assert queue_length(admin) == 2

    still_due = admin.execute(
        "select count(*) from public.conversations where pending_response_at is not null"
    ).fetchone()[0]
    assert still_due == 1

    # E o próximo tique pega o que sobrou — o limite atrasa, não descarta.
    assert len(coalesce(admin, limit=2)) == 1


# --- quem pode chamar -------------------------------------------------------


def test_the_runtime_can_coalesce(dsn: str, admin: psycopg.Connection, due_thread: Thread) -> None:
    with as_app_role(dsn, "worker_role", uuid.uuid4()) as conn:
        # Escopo de tenant irrelevante de propósito: a função é `security
        # definer` porque um processo coalesce para todos os tenants.
        assert len(coalesce(conn)) == 1
        conn.commit()


def test_the_data_api_cannot_coalesce(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("set role authenticated")

            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("select * from internal.coalesce_due_conversations()")

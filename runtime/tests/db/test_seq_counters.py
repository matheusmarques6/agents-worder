"""A2 — os contadores de sequência (ADR-6b).

`SELECT max(seq)+1` é proibido, e a proibição tem duas defesas: a trava de
fitness do E0-06, que reprova o padrão no código, e estes testes, que reprovam
o efeito. As duas mordem o mesmo erro por caminhos diferentes — uma lê o
código, a outra observa o banco sob concorrência.

O caminho oficial é `internal.next_message_seq()`, um `UPDATE ... RETURNING`
que é atômico por construção: a segunda conexão espera a primeira comitar e lê
o valor já incrementado.
"""

import threading
import uuid

import psycopg
import pytest

from tests.db.conftest import TwoTenants, as_app_role
from tests.db.factories import Thread, create_thread


@pytest.fixture
def thread(admin: psycopg.Connection, two_tenants: TwoTenants) -> Thread:
    return create_thread(admin, two_tenants.a.id)


def next_seq(conn: psycopg.Connection, conversation_id: uuid.UUID, direction: str) -> int:
    row = conn.execute(
        "select internal.next_message_seq(%s, %s)", (conversation_id, direction)
    ).fetchone()
    return row[0]


def test_the_first_sequence_of_a_conversation_is_one(
    admin: psycopg.Connection, thread: Thread
) -> None:
    # Não zero: `messages.seq` tem CHECK (seq > 0), e um contador que começasse
    # em 0 só falharia no primeiro INSERT de produção.
    assert next_seq(admin, thread.conversation_id, "inbound") == 1


def test_the_two_directions_count_independently(
    admin: psycopg.Connection, thread: Thread
) -> None:
    assert next_seq(admin, thread.conversation_id, "inbound") == 1
    assert next_seq(admin, thread.conversation_id, "outbound") == 1
    assert next_seq(admin, thread.conversation_id, "inbound") == 2


def test_two_concurrent_connections_get_distinct_consecutive_sequences(
    dsn: str, two_tenants: TwoTenants, thread: Thread
) -> None:
    """O teste que só falha sob concorrência — o motivo do ADR-6b existir.

    Duas conexões pedem a sequência ao mesmo tempo. Com `UPDATE ... RETURNING`
    saem 1 e 2, em alguma ordem. Com `SELECT max(seq)+1` sairiam 1 e 1, e o
    segundo INSERT violaria o UNIQUE — em produção, no meio de uma rajada.
    """
    barrier = threading.Barrier(2)
    results: list[int] = []
    lock = threading.Lock()

    def ask() -> None:
        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            barrier.wait(timeout=5)
            seq = next_seq(conn, thread.conversation_id, "inbound")
            conn.commit()
        with lock:
            results.append(seq)

    threads = [threading.Thread(target=ask) for _ in range(2)]
    for worker in threads:
        worker.start()
    for worker in threads:
        worker.join(timeout=10)

    assert sorted(results) == [1, 2]


def test_the_unique_constraint_cannot_be_violated_through_the_official_path(
    admin: psycopg.Connection, two_tenants: TwoTenants, thread: Thread
) -> None:
    # Vinte mensagens pelo caminho oficial. Se o contador entregasse um número
    # repetido, o UNIQUE (conversation_id, direction, seq) reprovaria aqui.
    for _ in range(20):
        seq = next_seq(admin, thread.conversation_id, "inbound")
        admin.execute(
            """
            insert into public.messages
                (tenant_id, conversation_id, direction, seq, channel, author_type, content)
            values (%s, %s, 'inbound', %s, 'whatsapp_cloud', 'contact', '{}'::jsonb)
            """,
            (two_tenants.a.id, thread.conversation_id, seq),
        )

    stored = admin.execute(
        "select seq from public.messages where conversation_id = %s order by seq",
        (thread.conversation_id,),
    ).fetchall()

    assert [row[0] for row in stored] == list(range(1, 21))


def test_an_unknown_direction_fails_loudly(admin: psycopg.Connection, thread: Thread) -> None:
    # Silenciosamente devolver o contador errado seria pior que um erro: as
    # duas direções são contadas separadamente e a troca só apareceria como um
    # buraco na numeração, meses depois.
    with pytest.raises(psycopg.errors.RaiseException):
        next_seq(admin, thread.conversation_id, "lateral")


def test_a_worker_cannot_advance_the_counter_of_another_tenant(
    dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
) -> None:
    # A função é `security invoker` justamente para isto: quem chama carrega o
    # seu escopo de RLS. Fosse `definer`, um worker comprometido mexeria na
    # numeração de qualquer loja.
    other = create_thread(admin, two_tenants.b.id)

    with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            next_seq(conn, other.conversation_id, "inbound")

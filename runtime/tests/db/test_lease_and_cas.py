"""A3 — lease e o compare-and-set estendido. O invariante central.

Três fases com transações curtas: reivindicar, trabalhar fora de qualquer
transação, concluir com um CAS que verifica quatro coisas ao mesmo tempo.

A quarta condição é a que dá nome ao invariante: uma mensagem que chega
**durante** a geração invalida o rascunho. As outras três detectam concorrência
entre workers; essa detecta concorrência com o CLIENTE — que é a que produz a
resposta constrangedora, respondendo à pergunta anterior depois de a pessoa já
ter mandado mais três.

Cada condição é testada FALHANDO ISOLADAMENTE. Um teste que quebra três
condições de uma vez passa com um CAS que verifica só uma.
"""

import uuid
from datetime import timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.db.conftest import TwoTenants, as_app_role
from tests.db.factories import create_thread, unique_id

REPLY = {"text": "seu pedido saiu para entrega"}


def claim(conn: psycopg.Connection, conversation_id: uuid.UUID, token: uuid.UUID) -> list[tuple]:
    return conn.execute(
        "select * from internal.claim_conversation(%s, %s)", (conversation_id, token)
    ).fetchall()


def conclude(
    conn: psycopg.Connection,
    conversation_id: uuid.UUID,
    *,
    token: uuid.UUID,
    version: int,
    generation: int,
    target_seq: int,
) -> tuple:
    return conn.execute(
        """
        select * from internal.conclude_turn(%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            conversation_id,
            token,
            version,
            generation,
            target_seq,
            Jsonb(REPLY),
            unique_id("idem"),
        ),
    ).fetchone()


def snapshot(conn: psycopg.Connection, conversation_id: uuid.UUID) -> dict:
    row = conn.execute(
        """
        select version, last_processed_seq, processing_token, processing_generation,
               next_inbound_seq
          from public.conversations where id = %s
        """,
        (conversation_id,),
    ).fetchone()
    return {
        "version": row[0],
        "last_processed_seq": row[1],
        "token": row[2],
        "generation": row[3],
        "next_inbound_seq": row[4],
    }


def side_effects(conn: psycopg.Connection, conversation_id: uuid.UUID) -> tuple[int, int]:
    outbound = conn.execute(
        "select count(*) from public.messages"
        " where conversation_id = %s and direction = 'outbound'",
        (conversation_id,),
    ).fetchone()[0]
    queued = conn.execute(
        "select count(*) from internal.message_outbox where conversation_id = %s",
        (conversation_id,),
    ).fetchone()[0]
    return outbound, queued


@pytest.fixture
def turn(admin: psycopg.Connection, two_tenants: TwoTenants) -> dict:
    """Uma conversa pronta para ser concluída: reivindicada, com um job corrente."""
    thread = create_thread(admin, two_tenants.a.id)
    admin.execute(
        """
        update public.conversations
           set processing_generation = 1, next_inbound_seq = 4
         where id = %s
        """,
        (thread.conversation_id,),
    )
    token = uuid.uuid4()
    (last_seq, version), = claim(admin, thread.conversation_id, token)

    return {
        "thread": thread,
        "token": token,
        "version": version,
        "generation": 1,
        "target_seq": 4,
        "last_processed_seq": last_seq,
    }


# --- FASE 1 · claim ---------------------------------------------------------


class TestTheClaim:
    def test_a_free_conversation_can_be_claimed(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        thread = create_thread(admin, two_tenants.a.id)

        claimed = claim(admin, thread.conversation_id, uuid.uuid4())

        assert claimed == [(0, 0)]

    def test_a_live_lease_yields_nothing_and_raises_nothing(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # Sem linha significa "outra task está processando", e a resposta certa
        # é desistir com um set_vt curto. Erro faria o worker tratar
        # concorrência normal como falha; espera seguraria uma conexão do pool
        # por dois minutos para descobrir o que já se sabe agora.
        thread = create_thread(admin, two_tenants.a.id)
        claim(admin, thread.conversation_id, uuid.uuid4())

        assert claim(admin, thread.conversation_id, uuid.uuid4()) == []

    def test_an_expired_lease_is_a_free_lease(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # É assim que o trabalho de um processo que morreu volta a ser feito,
        # sem ninguém precisar limpar nada.
        thread = create_thread(admin, two_tenants.a.id)
        admin.execute(
            """
            update public.conversations
               set processing_token = %s, processing_until = now() - interval '1 second'
             where id = %s
            """,
            (uuid.uuid4(), thread.conversation_id),
        )

        assert claim(admin, thread.conversation_id, uuid.uuid4()) == [(0, 0)]


class TestTheLeaseBelongsToItsOwner:
    def test_only_the_owner_renews(self, admin: psycopg.Connection, turn: dict) -> None:
        # Renovar a lease de outro é roubar a conversa no meio da resposta.
        assert (
            admin.execute(
                "select internal.renew_lease(%s, %s)",
                (turn["thread"].conversation_id, uuid.uuid4()),
            ).fetchone()[0]
            is False
        )
        assert (
            admin.execute(
                "select internal.renew_lease(%s, %s)",
                (turn["thread"].conversation_id, turn["token"]),
            ).fetchone()[0]
            is True
        )

    def test_only_the_owner_releases(self, admin: psycopg.Connection, turn: dict) -> None:
        # Sem esta condição, um worker atrasado liberaria a lease que um segundo
        # worker acabou de adquirir, e os dois responderiam.
        assert (
            admin.execute(
                "select internal.release_lease(%s, %s)",
                (turn["thread"].conversation_id, uuid.uuid4()),
            ).fetchone()[0]
            is False
        )
        assert snapshot(admin, turn["thread"].conversation_id)["token"] == turn["token"]

        assert (
            admin.execute(
                "select internal.release_lease(%s, %s)",
                (turn["thread"].conversation_id, turn["token"]),
            ).fetchone()[0]
            is True
        )
        assert snapshot(admin, turn["thread"].conversation_id)["token"] is None


# --- FASE 3 · o CAS, uma condição por vez -----------------------------------


class TestEachConditionAloneStopsTheConclusion:
    """Cada condição quebrada ISOLADAMENTE, com as outras três intactas.

    Quebrar duas de uma vez passaria num CAS que verifica só uma — e um CAS que
    verifica só uma é um motor de brinquedo.
    """

    def test_a_token_that_is_not_the_owner(
        self, admin: psycopg.Connection, turn: dict
    ) -> None:
        outcome = conclude(
            admin,
            turn["thread"].conversation_id,
            token=uuid.uuid4(),
            version=turn["version"],
            generation=turn["generation"],
            target_seq=turn["target_seq"],
        )

        assert outcome[0] is False
        assert side_effects(admin, turn["thread"].conversation_id) == (0, 0)

    def test_a_version_that_moved(self, admin: psycopg.Connection, turn: dict) -> None:
        outcome = conclude(
            admin,
            turn["thread"].conversation_id,
            token=turn["token"],
            version=turn["version"] + 1,
            generation=turn["generation"],
            target_seq=turn["target_seq"],
        )

        assert outcome[0] is False
        assert side_effects(admin, turn["thread"].conversation_id) == (0, 0)

    def test_a_generation_that_was_superseded(
        self, admin: psycopg.Connection, turn: dict
    ) -> None:
        # O coalescer gerou outro job por cima deste enquanto ele trabalhava.
        outcome = conclude(
            admin,
            turn["thread"].conversation_id,
            token=turn["token"],
            version=turn["version"],
            generation=turn["generation"] - 1,
            target_seq=turn["target_seq"],
        )

        assert outcome[0] is False
        assert side_effects(admin, turn["thread"].conversation_id) == (0, 0)

    def test_a_message_that_arrived_during_the_generation(
        self, admin: psycopg.Connection, turn: dict
    ) -> None:
        """A condição que dá nome ao invariante.

        O cliente mandou mais uma enquanto o agente escrevia. `next_inbound_seq`
        andou, o retrato que o coalescer tirou ficou velho, e a resposta que
        estava pronta responde à pergunta anterior. Ela não sai.
        """
        admin.execute(
            "update public.conversations set next_inbound_seq = next_inbound_seq + 1 where id = %s",
            (turn["thread"].conversation_id,),
        )

        outcome = conclude(
            admin,
            turn["thread"].conversation_id,
            token=turn["token"],
            version=turn["version"],
            generation=turn["generation"],
            target_seq=turn["target_seq"],
        )

        assert outcome[0] is False
        assert side_effects(admin, turn["thread"].conversation_id) == (0, 0)

    def test_the_lease_survives_a_failed_conclusion(
        self, admin: psycopg.Connection, turn: dict
    ) -> None:
        # O CAS que falha não solta a lease sozinho: quem chamou decide, com
        # `release_lease`, que só solta se ainda for o dono.
        conclude(
            admin,
            turn["thread"].conversation_id,
            token=turn["token"],
            version=turn["version"] + 1,
            generation=turn["generation"],
            target_seq=turn["target_seq"],
        )

        assert snapshot(admin, turn["thread"].conversation_id)["token"] == turn["token"]


class TestTheHappyPath:
    def test_the_conclusion_and_the_outbox_commit_together(
        self, admin: psycopg.Connection, turn: dict
    ) -> None:
        """Conclusão sem linha na outbox é a versão outbound do "prazo perdido".

        A conversa avança, `last_processed_seq` diz que já foi respondida, e o
        cliente nunca recebe nada. Nenhum alarme dispara, porque do ponto de
        vista do banco tudo deu certo.
        """
        conversation_id = turn["thread"].conversation_id

        outcome = conclude(
            admin,
            conversation_id,
            token=turn["token"],
            version=turn["version"],
            generation=turn["generation"],
            target_seq=turn["target_seq"],
        )

        assert outcome[0] is True
        assert outcome[1] == 1  # primeiro seq de saída da conversa

        assert side_effects(admin, conversation_id) == (1, 1)

        state = snapshot(admin, conversation_id)
        assert state["last_processed_seq"] == turn["target_seq"]
        assert state["version"] == turn["version"] + 1
        assert state["token"] is None

    def test_the_outbound_message_points_at_the_send_that_will_carry_it(
        self, admin: psycopg.Connection, turn: dict
    ) -> None:
        outcome = conclude(
            admin,
            turn["thread"].conversation_id,
            token=turn["token"],
            version=turn["version"],
            generation=turn["generation"],
            target_seq=turn["target_seq"],
        )

        linked = admin.execute(
            "select outbox_id, channel from public.messages"
            " where direction = 'outbound' and conversation_id = %s",
            (turn["thread"].conversation_id,),
        ).fetchone()

        assert linked == (outcome[2], "whatsapp_cloud")

    def test_a_second_conclusion_with_the_same_arguments_does_nothing(
        self, admin: psycopg.Connection, turn: dict
    ) -> None:
        # Reentrega do pgmq é comportamento normal. A segunda tentativa encontra
        # a lease já solta e a versão já andada — e para, sem duplicar o envio.
        arguments = {
            "token": turn["token"],
            "version": turn["version"],
            "generation": turn["generation"],
            "target_seq": turn["target_seq"],
        }
        conclude(admin, turn["thread"].conversation_id, **arguments)
        outcome = conclude(admin, turn["thread"].conversation_id, **arguments)

        assert outcome[0] is False
        assert side_effects(admin, turn["thread"].conversation_id) == (1, 1)


# --- quem executa -----------------------------------------------------------


class TestWhoMayRun:
    def test_the_worker_runs_the_whole_turn_under_its_own_tenant_scope(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        thread = create_thread(admin, two_tenants.a.id)
        token = uuid.uuid4()

        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            (last_seq, version), = claim(conn, thread.conversation_id, token)
            outcome = conclude(
                conn,
                thread.conversation_id,
                token=token,
                version=version,
                generation=0,
                target_seq=0,
            )
            conn.commit()

        assert (last_seq, version) == (0, 0)
        assert outcome[0] is True

    def test_a_worker_cannot_claim_a_conversation_of_another_tenant(
        self, dsn: str, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # `security invoker` é o que faz isto valer: a RLS continua no caminho.
        other = create_thread(admin, two_tenants.b.id)

        with as_app_role(dsn, "worker_role", two_tenants.a.id) as conn:
            assert claim(conn, other.conversation_id, uuid.uuid4()) == []

    def test_the_data_api_cannot_conclude_a_turn(self, dsn: str) -> None:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("set role authenticated")

                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(
                        "select internal.conclude_turn("
                        "gen_random_uuid(), gen_random_uuid(), 0, 0, 0, '{}'::jsonb, 'k')"
                    )


def test_the_lease_default_is_the_canonical_two_minutes(
    admin: psycopg.Connection, two_tenants: TwoTenants
) -> None:
    # O número canônico do CLAUDE.md, verificado onde ele é aplicado de verdade.
    thread = create_thread(admin, two_tenants.a.id)
    claim(admin, thread.conversation_id, uuid.uuid4())

    remaining = admin.execute(
        "select processing_until - now() from public.conversations where id = %s",
        (thread.conversation_id,),
    ).fetchone()[0]

    assert timedelta(seconds=110) < remaining <= timedelta(minutes=2)

"""Concluding a turn WITHOUT sending — the path Judge 1 needs (S8).

A draft the judge refused must never reach the outbox. But refusing is only
half the problem: if the turn does not conclude, `last_processed_seq` stays
behind, the coalescer creates the job again, and the agent regenerates against
the same message forever — burning tokens on a reply that will never be sent.

So the CAS has to be able to say "this turn is over and nothing goes out": the
sequence advances, the lease is released, the version moves, and the two
inserts (outbox and the outbound message) simply do not happen.

The function's own comment warns that a conclusion with no outbox row is "the
customer never receiving and nothing appearing anywhere". That is exactly right
for an ACCIDENT, and this path is the opposite of one: the responder writes the
alert BEFORE concluding, so either both exist or neither does.

Two shapes of "no content" are refused a send, on purpose. `psycopg`'s
`Jsonb(None)` renders JSON `null`, not SQL NULL — a caller who forgets that
would otherwise queue a message whose payload is the JSON value null, and the
sender would meet it at the worst possible moment.
"""

import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.db.conftest import TwoTenants
from tests.db.factories import create_thread, unique_id

REPLY = {"text": "seu pedido saiu para entrega"}


def conclude(
    conn: psycopg.Connection,
    conversation_id: uuid.UUID,
    *,
    token: uuid.UUID,
    version: int,
    generation: int,
    target_seq: int,
    content,
) -> tuple:
    return conn.execute(
        "select * from internal.conclude_turn(%s, %s, %s, %s, %s, %s, %s)",
        (
            conversation_id,
            token,
            version,
            generation,
            target_seq,
            content,
            unique_id("idem"),
        ),
    ).fetchone()


def snapshot(conn: psycopg.Connection, conversation_id: uuid.UUID) -> dict:
    row = conn.execute(
        """
        select version, last_processed_seq, processing_token, processing_until
          from public.conversations where id = %s
        """,
        (conversation_id,),
    ).fetchone()
    return {
        "version": row[0],
        "last_processed_seq": row[1],
        "token": row[2],
        "until": row[3],
    }


def side_effects(conn: psycopg.Connection, conversation_id: uuid.UUID) -> tuple[int, int]:
    """(outbound messages, outbox rows) — what a send leaves behind."""
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
    """A conversation claimed and ready to be concluded."""
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
    ((_, version),) = admin.execute(
        "select * from internal.claim_conversation(%s, %s)", (thread.conversation_id, token)
    ).fetchall()

    return {
        "conversation_id": thread.conversation_id,
        "token": token,
        "version": version,
        "generation": 1,
        "target_seq": 4,
    }


class TestTheBlockedTurn:
    @pytest.mark.parametrize(
        ("label", "content"),
        [("sql null", None), ("json null", Jsonb(None))],
        ids=["sql-null", "json-null"],
    )
    def test_nothing_is_queued_and_nothing_is_said(
        self, admin: psycopg.Connection, turn: dict, label: str, content
    ) -> None:
        committed, outbound_seq, outbox_id = conclude(
            admin,
            turn["conversation_id"],
            token=turn["token"],
            version=turn["version"],
            generation=turn["generation"],
            target_seq=turn["target_seq"],
            content=content,
        )

        assert committed is True, f"{label}: the turn is over, even with nothing to say"
        assert (outbound_seq, outbox_id) == (None, None)
        assert side_effects(admin, turn["conversation_id"]) == (0, 0)

    def test_the_conversation_moves_on(self, admin: psycopg.Connection, turn: dict) -> None:
        """The half that stops the infinite regeneration: the sequence advances
        and the lease is released, so the coalescer has nothing left to redo."""
        conclude(
            admin,
            turn["conversation_id"],
            token=turn["token"],
            version=turn["version"],
            generation=turn["generation"],
            target_seq=turn["target_seq"],
            content=None,
        )

        after = snapshot(admin, turn["conversation_id"])
        assert after["last_processed_seq"] == turn["target_seq"]
        assert after["version"] == turn["version"] + 1
        assert after["token"] is None
        assert after["until"] is None

    def test_a_stale_cas_is_still_refused(self, admin: psycopg.Connection, turn: dict) -> None:
        """Blocking a draft does not weaken the CAS: a turn whose generation has
        moved on concludes nothing at all, with or without content."""
        before = snapshot(admin, turn["conversation_id"])

        committed, _, _ = conclude(
            admin,
            turn["conversation_id"],
            token=turn["token"],
            version=turn["version"],
            generation=turn["generation"] + 1,
            target_seq=turn["target_seq"],
            content=None,
        )

        assert committed is False
        assert snapshot(admin, turn["conversation_id"]) == before


class TestTheOrdinaryTurn:
    def test_a_reply_still_reaches_the_outbox_and_the_transcript(
        self, admin: psycopg.Connection, turn: dict
    ) -> None:
        """The N-1 guard: the same function, the same signature, and a caller
        that passes content behaves exactly as it did before S8."""
        committed, outbound_seq, outbox_id = conclude(
            admin,
            turn["conversation_id"],
            token=turn["token"],
            version=turn["version"],
            generation=turn["generation"],
            target_seq=turn["target_seq"],
            content=Jsonb(REPLY),
        )

        assert committed is True
        assert outbound_seq == 1
        assert outbox_id is not None
        assert side_effects(admin, turn["conversation_id"]) == (1, 1)

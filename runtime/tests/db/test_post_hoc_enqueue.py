"""Who enqueues the evaluation, and how a correction gets out (S9b, decisão 91).

The property this file guards is narrow and load-bearing: **the evaluation job
exists exactly when the reply exists**. Not when a draft was written — drafts
die at the CAS — and not a moment later in another transaction, where a crash
would leave a sent message nobody ever judged.

The three enqueue tests are the same assertion from three directions: sent →
one job; blocked by Judge 1 → none; refused by the CAS → none. Only the first
is the happy path, and it is the least interesting of the three.
"""

import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.db.conftest import TwoTenants
from tests.db.factories import create_thread, unique_id

pytestmark = pytest.mark.db

REPLY = {"text": "seu pedido saiu para entrega"}
CORRECTION = {"text": "corrigindo: o prazo é de 5 dias úteis, não 3"}


def conclude(conn: psycopg.Connection, turn: dict, *, content, version_delta: int = 0) -> tuple:
    return conn.execute(
        "select * from internal.conclude_turn(%s, %s, %s, %s, %s, %s, %s)",
        (
            turn["conversation_id"],
            turn["token"],
            turn["version"] + version_delta,
            turn["generation"],
            turn["target_seq"],
            content,
            unique_id("idem"),
        ),
    ).fetchone()


def eval_jobs(conn: psycopg.Connection, conversation_id: uuid.UUID) -> list[dict]:
    """The jobs waiting in `q_evals` for this conversation. Scoped by id rather
    than counted globally: the queue is shared state and another test's leftover
    is not this test's business."""
    rows = conn.execute(
        "select message from pgmq.q_q_evals where message->>'conversation_id' = %s",
        (str(conversation_id),),
    ).fetchall()
    return [row[0] for row in rows]


@pytest.fixture
def turn(admin: psycopg.Connection, two_tenants: TwoTenants) -> dict:
    thread = create_thread(admin, two_tenants.a.id)
    admin.execute(
        "update public.conversations"
        " set processing_generation = 1, next_inbound_seq = 4 where id = %s",
        (thread.conversation_id,),
    )
    token = uuid.uuid4()
    ((_, version),) = admin.execute(
        "select * from internal.claim_conversation(%s, %s)", (thread.conversation_id, token)
    ).fetchall()
    return {
        "conversation_id": thread.conversation_id,
        "tenant_id": two_tenants.a.id,
        "token": token,
        "version": version,
        "generation": 1,
        "target_seq": 4,
    }


class TestTheEvaluationJob:
    def test_a_sent_reply_enqueues_exactly_one_evaluation(
        self, admin: psycopg.Connection, turn: dict
    ) -> None:
        committed, _, _ = conclude(admin, turn, content=Jsonb(REPLY))
        assert committed

        (job,) = eval_jobs(admin, turn["conversation_id"])

        # Ids only (decisão 74): everything else lives on the row, and a copy in
        # the payload is a copy that can drift.
        assert job["tenant_id"] == str(turn["tenant_id"])
        message_id = admin.execute(
            "select id from public.messages"
            " where conversation_id = %s and direction = 'outbound'",
            (turn["conversation_id"],),
        ).fetchone()[0]
        assert job["message_id"] == str(message_id), (
            "the job must point at the message that was actually sent — anything "
            "else and the judge would read a different reply than the customer got"
        )

    @pytest.mark.parametrize("content", [None, Jsonb(None)], ids=["sql-null", "json-null"])
    def test_a_reply_the_judge_blocked_enqueues_nothing(
        self, admin: psycopg.Connection, turn: dict, content
    ) -> None:
        """Nothing was said, so there is nothing to judge. A job here would send
        the post-hoc judge to read a message that does not exist."""
        committed, _, _ = conclude(admin, turn, content=content)
        assert committed

        assert eval_jobs(admin, turn["conversation_id"]) == []

    def test_a_draft_the_cas_refused_enqueues_nothing(
        self, admin: psycopg.Connection, turn: dict
    ) -> None:
        """The central invariant, seen from the evaluation side: a newer message
        killed this draft, so it never reached anyone. Evaluating it would let a
        `critical` verdict send a correction for a message the customer never
        received (decisão 91)."""
        committed, _, _ = conclude(admin, turn, content=Jsonb(REPLY), version_delta=99)
        assert not committed

        assert eval_jobs(admin, turn["conversation_id"]) == []


class TestTheCorrection:
    @pytest.fixture
    def sent_message_id(self, admin: psycopg.Connection, turn: dict) -> uuid.UUID:
        conclude(admin, turn, content=Jsonb(REPLY))
        return admin.execute(
            "select id from public.messages"
            " where conversation_id = %s and direction = 'outbound'",
            (turn["conversation_id"],),
        ).fetchone()[0]

    def test_a_correction_becomes_an_ordinary_outbound(
        self, admin: psycopg.Connection, turn: dict, sent_message_id: uuid.UUID
    ) -> None:
        """It leaves through the outbox and the sender like any other message —
        there is no private channel for corrections."""
        (outcome,) = admin.execute(
            "select internal.enqueue_correction(%s, %s)", (sent_message_id, Jsonb(CORRECTION))
        ).fetchone()
        assert outcome == "sent"

        kind, key, payload = admin.execute(
            "select kind, idempotency_key, payload from internal.message_outbox"
            " where conversation_id = %s and kind = 'correction'",
            (turn["conversation_id"],),
        ).fetchone()
        assert kind == "correction"
        assert key == f"correction-{sent_message_id}"
        assert payload == CORRECTION

        # And it is in the transcript: a correction the customer received but the
        # history does not show would make every later turn read the wrong past.
        assert (
            admin.execute(
                "select count(*) from public.messages"
                " where conversation_id = %s and direction = 'outbound'",
                (turn["conversation_id"],),
            ).fetchone()[0]
            == 2
        )

    def test_a_redelivered_job_cannot_correct_the_same_message_twice(
        self, admin: psycopg.Connection, turn: dict, sent_message_id: uuid.UUID
    ) -> None:
        """pgmq redelivers; the customer must not receive the correction twice.
        The guard is the outbox UNIQUE keyed by the corrected message — the same
        second lock the funnel touch uses (decisão 74)."""
        admin.execute(
            "select internal.enqueue_correction(%s, %s)", (sent_message_id, Jsonb(CORRECTION))
        )

        (outcome,) = admin.execute(
            "select internal.enqueue_correction(%s, %s)", (sent_message_id, Jsonb(CORRECTION))
        ).fetchone()

        assert outcome == "already_sent", "a second correction is a duplicate, not an error"
        assert (
            admin.execute(
                "select count(*) from internal.message_outbox"
                " where conversation_id = %s and kind = 'correction'",
                (turn["conversation_id"],),
            ).fetchone()[0]
            == 1
        )

    def test_correcting_a_message_that_does_not_exist_is_a_bug_not_an_outcome(
        self, admin: psycopg.Connection
    ) -> None:
        """Outcomes are data for what the world can legitimately be. A job
        pointing at no message is a defect in whoever enqueued it, and the retry
        ladder to the DLQ is where a human sees it."""
        with pytest.raises(psycopg.errors.RaiseException):
            admin.execute(
                "select internal.enqueue_correction(%s, %s)",
                (uuid.uuid4(), Jsonb(CORRECTION)),
            )

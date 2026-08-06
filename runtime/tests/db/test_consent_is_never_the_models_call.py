"""O botão do RF-033(a) atravessa o turno de entrada SEM passar pelo modelo.

This is the security half of via (a), and it is the assertion the step exists
for. `CLAUDE.md` says a contact's message is hostile input to the LLM: a message
from a contact never changes rules and never enables anything outside the
tenant's set. A tap on Bloquear is the one message in this product that DOES
change a rule — so it is the one message whose effect may not depend on a model
having read it, agreed with it, been available, or been configured at all.

The proof is by non-dependence, three ways:

  * a responder that answers something bland and calls no tool — the block still
    lands. If recognition were delegated to the model this is exactly what would
    silently do nothing;
  * a responder that RAISES — the block still lands. The turn dies, the job
    climbs to the DLQ, and the contact's refusal is already a row;
  * a tenant whose agent could not run at all is the same shape as the second.

And the negative that keeps the feature honest: an ordinary message writes
nothing. A path that suppressed on anything other than an id we issued would be
a path that suppresses on a sentence.
"""

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agents_runtime.config import QueueingConfig
from agents_runtime.dispatch import consent
from agents_runtime.queueing.jobs import InboundJob
from agents_runtime.queueing.worker import run_turn
from tests.db.factories import Thread, create_tenant, create_thread
from tests.support.clock import FrozenClock
from tests.support.database import as_runtime_worker
from tests.support.llm import START

CONFIG = QueueingConfig()


@pytest.fixture
def tenant(admin: psycopg.Connection) -> Iterator[uuid.UUID]:
    tenant_id = create_tenant(admin)
    yield tenant_id
    with admin.cursor() as cur:
        cur.execute("delete from public.tenants where id = %s", (tenant_id,))


def _arrives(
    admin: psycopg.Connection, tenant_id: uuid.UUID, thread: Thread, content: dict
) -> int:
    """One inbound message, exactly as the ingestion would have written it.

    The sequence comes from the atomic counter, never from `max(seq)+1`.
    """
    with admin.cursor() as cur:
        cur.execute(
            "select internal.next_message_seq(%s, 'inbound')", (thread.conversation_id,)
        )
        (seq,) = cur.fetchone()
        cur.execute(
            """
            insert into public.messages
                (tenant_id, conversation_id, direction, seq, channel, author_type, content)
            values (%s, %s, 'inbound', %s, 'whatsapp_cloud', 'contact', %s)
            """,
            (tenant_id, thread.conversation_id, seq, Jsonb(content)),
        )
    return seq


def _tap(button_id: str) -> dict:
    return {"type": "interactive", "text": None, "button_reply": {"id": button_id}}


def _state(admin: psycopg.Connection, contact_id: uuid.UUID) -> tuple:
    with admin.cursor() as cur:
        cur.execute(
            """
            select (select opt_status from public.contacts where id = %s),
                   (select reason from public.suppression_list where contact_id = %s)
            """,
            (contact_id, contact_id),
        )
        return cur.fetchone()


async def _turn(dsn: str, tenant_id: uuid.UUID, thread: Thread, seq: int, respond) -> None:
    job = InboundJob(
        conversation_id=thread.conversation_id,
        generation=1,
        target_seq=seq,
        tenant_id=tenant_id,
    )
    async with as_runtime_worker(dsn) as conn:
        await run_turn(conn, job, respond, config=CONFIG, clock=FrozenClock(START))


async def _silent_agent(job: InboundJob) -> dict:
    """An agent that answers and consults nothing. If the consent decision went
    through the model, this responder would be the one that loses it."""
    return {"text": "Claro! Posso ajudar com mais alguma coisa?"}


async def _broken_agent(job: InboundJob) -> dict:
    raise RuntimeError("o provedor caiu no meio do turno")


class TestTheBlockDoesNotDependOnTheModel:
    async def test_a_responder_that_calls_no_tool_still_blocks(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        thread = create_thread(admin, tenant)
        seq = _arrives(admin, tenant, thread, _tap(consent.BLOCK_BUTTON_ID))

        await _turn(dsn, tenant, thread, seq, _silent_agent)

        assert _state(admin, thread.contact_id) == ("blocked", "explicit_block")

    async def test_a_turn_that_dies_in_the_model_still_blocks(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """The refusal is a row before the model is even called. The turn takes
        the retry ladder to the DLQ, as it should — and the contact does not
        keep receiving funnels while a human reads it."""
        thread = create_thread(admin, tenant)
        seq = _arrives(admin, tenant, thread, _tap(consent.BLOCK_BUTTON_ID))

        with pytest.raises(RuntimeError):
            await _turn(dsn, tenant, thread, seq, _broken_agent)

        assert _state(admin, thread.contact_id) == ("blocked", "explicit_block")

    async def test_the_block_is_recorded_with_the_platform_as_author(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """`system`, not `agent`: nobody's judgement was involved. The trail has
        to be able to tell an instruction we obeyed from a sentence a model
        read (via c), because only one of the two can ever be wrong about what
        the contact meant."""
        thread = create_thread(admin, tenant)
        seq = _arrives(admin, tenant, thread, _tap(consent.BLOCK_BUTTON_ID))

        await _turn(dsn, tenant, thread, seq, _silent_agent)

        with admin.cursor() as cur:
            cur.execute(
                """
                select created_by from public.suppression_list where contact_id = %s
                """,
                (thread.contact_id,),
            )
            assert cur.fetchone() == ("system",)
            cur.execute(
                """
                select action from public.audit_log
                 where target_type = 'contact' and target_id = %s
                """,
                (thread.contact_id,),
            )
            assert cur.fetchall() == [("suppression.explicit_block",)]


class TestAutorizar:
    async def test_the_other_button_records_the_consent(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        thread = create_thread(admin, tenant)
        seq = _arrives(admin, tenant, thread, _tap(consent.AUTHORIZE_BUTTON_ID))

        await _turn(dsn, tenant, thread, seq, _silent_agent)

        assert _state(admin, thread.contact_id) == ("authorized", None)

    async def test_a_tap_on_autorizar_never_undoes_a_suppression(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """A suppressed contact receives no proactive, so no buttons — meaning
        an Autorizar arriving for one is a stale tap or a forged id. Either way
        the answer is the same: a tap does not erase the record of somebody
        having asked not to be messaged. Unblocking is an operator action, with
        a screen and a trail (E5)."""
        thread = create_thread(admin, tenant)
        with admin.cursor() as cur:
            cur.execute(
                """
                insert into public.suppression_list (tenant_id, contact_id, reason, created_by)
                values (%s, %s, 'intent_optout', 'agent')
                """,
                (tenant, thread.contact_id),
            )
        seq = _arrives(admin, tenant, thread, _tap(consent.AUTHORIZE_BUTTON_ID))

        await _turn(dsn, tenant, thread, seq, _silent_agent)

        assert _state(admin, thread.contact_id) == ("blocked", "intent_optout")


class TestWhatDoesNotMoveConsent:
    async def test_an_ordinary_message_writes_nothing(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        thread = create_thread(admin, tenant)
        seq = _arrives(
            admin, tenant, thread, {"type": "text", "text": "quando chega meu pedido?"}
        )

        await _turn(dsn, tenant, thread, seq, _silent_agent)

        assert _state(admin, thread.contact_id) == ("pending", None)

    async def test_a_sentence_that_reads_like_an_opt_out_is_still_not_a_tap(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """Via (c) owns this sentence, and via (c) needs a model. A
        deterministic path that suppressed on words would be a keyword list in
        charge of consent — wrong in both directions, and impossible to argue
        with."""
        thread = create_thread(admin, tenant)
        seq = _arrives(
            admin, tenant, thread, {"type": "text", "text": "PARE de me mandar mensagem"}
        )

        await _turn(dsn, tenant, thread, seq, _silent_agent)

        assert _state(admin, thread.contact_id) == ("pending", None)

    async def test_the_last_tap_of_a_burst_is_the_one_that_counts(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """Both taps land inside one debounce window and reach the turn
        together. What a person meant is what they did last — and reading it the
        other way would let a mis-tap on Bloquear be undone only by luck of
        ordering."""
        thread = create_thread(admin, tenant)
        _arrives(admin, tenant, thread, _tap(consent.AUTHORIZE_BUTTON_ID))
        seq = _arrives(admin, tenant, thread, _tap(consent.BLOCK_BUTTON_ID))

        await _turn(dsn, tenant, thread, seq, _silent_agent)

        assert _state(admin, thread.contact_id) == ("blocked", "explicit_block")

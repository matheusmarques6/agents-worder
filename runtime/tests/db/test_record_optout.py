"""RF-033 via (c) — o modelo detecta a intenção, a tool executa o efeito.

The division of labour is the property under test, and it is a security
property rather than a tidiness one. Reading "para de me mandar mensagem" out
of a sentence is a judgement about language; deciding WHOSE contact disappears
from every future funnel is authorisation. This tool does the second and takes
nothing about it from the first: the contact is resolved from the conversation
the job is about, under the tenant's own RLS, and the model's arguments are read
for nothing at all.

Three things a passing suite here has to mean:

  * the row exists and the trail exists — RNF-044 asks for consent AND
    opposition registered, with reason and timestamp, and `internal.tool_calls`
    plus `public.audit_log` are the two halves of that;
  * a conversation the tenant does not own produces no row anywhere. Not a
    partial write, not an empty success;
  * **opt-out suppresses sending, it does not erase data.** The messages, the
    conversation and the contact are all still there afterwards. This is the
    assertion that would fail the day somebody reads "remove da lista" as
    "delete the contact".

The connection is the one `app.py` builds — autocommit, role set, and NO tenant
scope — because a pre-scoped connection would hide a tool that forgot to scope
itself.
"""

import uuid

import psycopg
import pytest

from agents_runtime.tools import base as tools
from agents_runtime.tools.consent import RecordOptout
from agents_runtime.tools.registry import MANDATORY, build_toolset
from tests.db.factories import create_message, create_tenant, create_thread
from tests.support.clock import FrozenClock
from tests.support.database import as_runtime_worker
from tests.support.llm import START, EmbeddingStandIn


@pytest.fixture
def tenant(admin: psycopg.Connection) -> uuid.UUID:
    tenant_id = create_tenant(admin)
    yield tenant_id
    with admin.cursor() as cur:
        cur.execute("delete from public.tenants where id = %s", (tenant_id,))


def _context(tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> tools.ToolContext:
    return tools.ToolContext(tenant_id=tenant_id, conversation_id=conversation_id)


async def _run(dsn: str, context: tools.ToolContext, arguments: dict | None = None):
    async with as_runtime_worker(dsn) as conn:
        return await tools.run_tool(
            conn, RecordOptout(), context, arguments or {}, clock=FrozenClock(START)
        )


def _suppressions(admin: psycopg.Connection, contact_id: uuid.UUID) -> list[tuple]:
    with admin.cursor() as cur:
        cur.execute(
            "select reason, created_by from public.suppression_list where contact_id = %s",
            (contact_id,),
        )
        return cur.fetchall()


def _audit(admin: psycopg.Connection, contact_id: uuid.UUID) -> list[tuple]:
    with admin.cursor() as cur:
        cur.execute(
            """
            select action, actor_type, payload
              from public.audit_log
             where target_type = 'contact' and target_id = %s
             order by id
            """,
            (contact_id,),
        )
        return cur.fetchall()


class TestTheToolPerformsTheOptOut:
    async def test_it_writes_the_suppression_with_the_agents_reason(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        thread = create_thread(admin, tenant)

        result = await _run(dsn, _context(tenant, thread.conversation_id))

        assert result.success is True
        assert result.output == {
            "suppressed": True,
            "already": False,
            "reason": "intent_optout",
        }
        # `intent_optout` and not `manual`: the ladder maps the reason to
        # `suppressed_optout`, and an operator reading "manual" would go looking
        # for a human who never touched this.
        assert _suppressions(admin, thread.contact_id) == [("intent_optout", "agent")]

    async def test_the_opposition_reaches_the_audit_trail(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """RNF-044. `suppression_list` is the state; this is the record of who
        caused it, and it is append-only by privilege."""
        thread = create_thread(admin, tenant)

        await _run(dsn, _context(tenant, thread.conversation_id))

        (entry,) = _audit(admin, thread.contact_id)
        action, actor_type, payload = entry
        assert action == "suppression.intent_optout"
        assert actor_type == "system"
        assert payload == {"reason": "intent_optout", "created_by": "agent"}

    async def test_every_execution_leaves_a_row_in_tool_calls(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        thread = create_thread(admin, tenant)

        await _run(dsn, _context(tenant, thread.conversation_id))

        with admin.cursor() as cur:
            cur.execute(
                "select tool_name, success, tenant_id from internal.tool_calls"
                " where conversation_id = %s",
                (thread.conversation_id,),
            )
            assert cur.fetchall() == [("record_optout", True, tenant)]

    async def test_asking_twice_is_the_same_answer_and_one_record(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """A contact who repeats the request is not an error. The state is one
        row — suppression is a state, not a log — and the trail does not grow a
        second entry for a fact that did not change."""
        thread = create_thread(admin, tenant)

        first = await _run(dsn, _context(tenant, thread.conversation_id))
        second = await _run(dsn, _context(tenant, thread.conversation_id))

        assert first.output["already"] is False
        assert second.output == {"suppressed": True, "already": True, "reason": "intent_optout"}
        assert len(_suppressions(admin, thread.contact_id)) == 1
        assert len(_audit(admin, thread.contact_id)) == 1


class TestItSuppressesSendingAndNothingElse:
    async def test_the_conversation_and_its_messages_survive_the_opt_out(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """RNF-044: opt-out suppresses sending, it does not erase data. The
        contact still gets support when THEY write — what stops is us starting
        the conversation."""
        thread = create_thread(admin, tenant)
        create_message(admin, tenant, thread, seq=1, text="não quero mais nada de vocês")

        await _run(dsn, _context(tenant, thread.conversation_id))

        with admin.cursor() as cur:
            cur.execute(
                """
                select (select count(*) from public.contacts where id = %s),
                       (select count(*) from public.conversations where id = %s),
                       (select count(*) from public.messages where conversation_id = %s)
                """,
                (thread.contact_id, thread.conversation_id, thread.conversation_id),
            )
            assert cur.fetchone() == (1, 1, 1)

    async def test_the_projection_follows_the_authority(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """S6's architecture decision, seen from the outside: the tool writes
        `suppression_list` and never touches `contacts.opt_status`, yet the
        column is right afterwards. Two places answering "is this contact
        blocked?" diverge on the worst day; here one of them is derived."""
        thread = create_thread(admin, tenant)

        await _run(dsn, _context(tenant, thread.conversation_id))

        with admin.cursor() as cur:
            cur.execute(
                "select opt_status from public.contacts where id = %s", (thread.contact_id,)
            )
            assert cur.fetchone() == ("blocked",)


class TestScopeNeverComesFromTheModel:
    async def test_a_conversation_of_another_tenant_writes_nothing(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """The conversation id is the one place this tool could be pointed
        elsewhere, and a stranger's conversation simply does not exist for a
        connection scoped to us. Nothing partial: no suppression, no trail."""
        stranger = create_tenant(admin)
        try:
            theirs = create_thread(admin, stranger)

            result = await _run(dsn, _context(tenant, theirs.conversation_id))

            assert result.success is False
            assert _suppressions(admin, theirs.contact_id) == []
            assert _audit(admin, theirs.contact_id) == []
        finally:
            with admin.cursor() as cur:
                cur.execute("delete from public.tenants where id = %s", (stranger,))

    async def test_a_contact_id_in_the_arguments_is_ignored(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """The model chose these arguments after reading a message written by a
        stranger. A contact id among them is not a hint — it is an attempt to
        aim the tool, and the answer is to suppress the contact of THIS
        conversation and nobody else's."""
        thread = create_thread(admin, tenant)
        other = create_thread(admin, tenant)

        await _run(
            dsn,
            _context(tenant, thread.conversation_id),
            {"contact_id": str(other.contact_id), "tenant_id": str(uuid.uuid4())},
        )

        assert _suppressions(admin, thread.contact_id) == [("intent_optout", "agent")]
        assert _suppressions(admin, other.contact_id) == []


class TestTheToolIsNotAChoice:
    def test_the_agent_always_holds_it(self) -> None:
        """The same principle that makes the Judge 1 model platform-fixed: a
        safety gate does not weaken by customer configuration. A merchant who
        could disable this would be disabling somebody else's right to refuse.
        """
        assert MANDATORY == ("record_optout",)
        assert "record_optout" in build_toolset((), embedder=EmbeddingStandIn())
        assert "record_optout" in build_toolset(
            ("search_knowledge",), embedder=EmbeddingStandIn()
        )

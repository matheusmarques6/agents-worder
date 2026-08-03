"""The toolset of the E2 — self-validating, short-transactioned, recorded.

A tool is the one place where text a contact wrote reaches the database. The
model decides WHICH tool to call and with what arguments, and it decides that
after reading a message written by a stranger — so nothing a tool does about
authorisation may depend on what the model said. Three properties follow, and
each is a test here:

  * **scope never comes from the arguments.** A `tenant_id` in the model's
    arguments is ignored, always: the scope comes from the job's context.
  * **no transaction ever spans the embedding call.** ADR-6 holds inside the
    responder too, and a retrieval that embedded inside its own transaction
    would hold a connection open across a network call to a provider.
  * **every execution is recorded**, success or failure, in `internal.tool_calls`
    — including the failures, which is the whole reason a merchant can be told
    "the agent tried to look this up and could not".

The connection here is shaped like the one `app.py` builds (autocommit, role
set, no tenant scope): if a tool forgot to scope itself, a pre-scoped
connection would hide it.
"""

import uuid

import psycopg
import pytest

from agents_runtime.agent_core.llm import EmbeddingResult, Usage
from agents_runtime.tools import base as tools
from agents_runtime.tools.customer import GetCustomerContext
from agents_runtime.tools.knowledge import SearchKnowledge
from tests.db.factories import create_tenant, create_thread
from tests.support.clock import FrozenClock
from tests.support.database import as_platform, as_runtime_worker
from tests.support.embedding import embed_text
from tests.support.llm import START, EmbeddingStandIn

FRETE = "Frete grátis para compras acima de duzentos reais."
TROCA = "Trocas em até sete dias corridos."


@pytest.fixture
def tenant(admin: psycopg.Connection) -> uuid.UUID:
    tenant_id = create_tenant(admin)
    yield tenant_id
    with admin.cursor() as cur:
        cur.execute("delete from public.tenants where id = %s", (tenant_id,))


async def _ingest(dsn: str, tenant_id: uuid.UUID, texts=(FRETE, TROCA)) -> None:
    from agents_runtime.repository import knowledge as knowledge_repo

    async with as_platform(dsn) as conn:
        for text in texts:
            await knowledge_repo.ingest_chunk(
                conn,
                tenant_id=tenant_id,
                source="faq",
                content=text,
                embedding=embed_text(text),
            )
        await conn.commit()


def _context(tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> tools.ToolContext:
    return tools.ToolContext(tenant_id=tenant_id, conversation_id=conversation_id)


async def _recorded(admin: psycopg.Connection, conversation_id: uuid.UUID) -> list[tuple]:
    with admin.cursor() as cur:
        cur.execute(
            """
            select tool_name, input, output, success, error, latency_ms, tenant_id
              from internal.tool_calls where conversation_id = %s order by id
            """,
            (conversation_id,),
        )
        return cur.fetchall()


class TestSearchKnowledge:
    async def test_it_hands_the_agent_the_chunk_that_answers(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        thread = create_thread(admin, tenant)
        await _ingest(dsn, tenant)

        async with as_runtime_worker(dsn) as conn:
            result = await tools.run_tool(
                conn,
                SearchKnowledge(EmbeddingStandIn()),
                _context(tenant, thread.conversation_id),
                {"query": "quanto custa o frete?"},
                clock=FrozenClock(START),
            )

        assert result.success is True
        assert result.output["chunks"][0]["content"] == FRETE

    async def test_the_scope_never_comes_from_the_arguments(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """The model read a message from a stranger before choosing these
        arguments. A `tenant_id` among them is not a hint — it is an attack, and
        the only answer is to ignore it and scope from the job."""
        stranger = create_tenant(admin)
        try:
            thread = create_thread(admin, tenant)
            await _ingest(dsn, tenant, texts=(FRETE,))
            await _ingest(dsn, stranger, texts=("Cupom secreto do outro lojista.",))

            async with as_runtime_worker(dsn) as conn:
                result = await tools.run_tool(
                    conn,
                    SearchKnowledge(EmbeddingStandIn()),
                    _context(tenant, thread.conversation_id),
                    {"query": "cupom secreto", "tenant_id": str(stranger)},
                    clock=FrozenClock(START),
                )

            contents = [chunk["content"] for chunk in result.output["chunks"]]
            assert contents == [FRETE]
        finally:
            with admin.cursor() as cur:
                cur.execute("delete from public.tenants where id = %s", (stranger,))

    async def test_no_transaction_is_held_across_the_embedding_call(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """ADR-6 inside the responder: the embedding is a call to a provider,
        and a transaction open across it is a connection held hostage by
        somebody else's latency."""
        thread = create_thread(admin, tenant)
        await _ingest(dsn, tenant, texts=(FRETE,))
        seen: list[psycopg.pq.TransactionStatus] = []

        async with as_runtime_worker(dsn) as conn:
            standin = EmbeddingStandIn(watching=conn, seen=seen)
            await tools.run_tool(
                conn,
                SearchKnowledge(standin),
                _context(tenant, thread.conversation_id),
                {"query": "frete"},
                clock=FrozenClock(START),
            )

        assert seen == [psycopg.pq.TransactionStatus.IDLE]

    async def test_an_argument_that_is_not_a_query_is_refused(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """Strict parsing, the webhook doctrine: never "use what parsed"."""
        thread = create_thread(admin, tenant)

        async with as_runtime_worker(dsn) as conn:
            result = await tools.run_tool(
                conn,
                SearchKnowledge(EmbeddingStandIn()),
                _context(tenant, thread.conversation_id),
                {"query": 42},
                clock=FrozenClock(START),
            )

        assert result.success is False
        assert "query" in result.error


class TestGetCustomerContext:
    async def test_it_reads_the_contact_of_this_conversation(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        thread = create_thread(admin, tenant)
        with admin.cursor() as cur:
            cur.execute(
                """
                update public.contacts
                   set name = 'Joana', language = 'pt-BR', opt_status = 'authorized'
                 where id = %s
                """,
                (thread.contact_id,),
            )

        async with as_runtime_worker(dsn) as conn:
            result = await tools.run_tool(
                conn,
                GetCustomerContext(),
                _context(tenant, thread.conversation_id),
                {},
                clock=FrozenClock(START),
            )

        assert result.success is True
        assert result.output["name"] == "Joana"
        assert result.output["language"] == "pt-BR"
        assert result.output["opt_status"] == "authorized"
        assert result.output["conversations"] == 1

    async def test_it_never_reads_a_conversation_of_another_tenant(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """The conversation id is the one place a tool could be pointed
        elsewhere. Scoped by the policy, a stranger's conversation is simply not
        there — and the tool says so instead of inventing a customer."""
        stranger = create_tenant(admin)
        try:
            theirs = create_thread(admin, stranger)

            async with as_runtime_worker(dsn) as conn:
                result = await tools.run_tool(
                    conn,
                    GetCustomerContext(),
                    _context(tenant, theirs.conversation_id),
                    {},
                    clock=FrozenClock(START),
                )

            assert result.success is False
            assert result.output == {}
        finally:
            with admin.cursor() as cur:
                cur.execute("delete from public.tenants where id = %s", (stranger,))


class TestTheTrail:
    async def test_every_execution_leaves_a_row(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        thread = create_thread(admin, tenant)
        await _ingest(dsn, tenant, texts=(FRETE,))
        clock = FrozenClock(START)

        async with as_runtime_worker(dsn) as conn:
            await tools.run_tool(
                conn,
                SearchKnowledge(EmbeddingStandIn(clock=clock)),
                _context(tenant, thread.conversation_id),
                {"query": "frete"},
                clock=clock,
            )

        (row,) = await _recorded(admin, thread.conversation_id)
        tool_name, recorded_input, output, success, error, latency_ms, owner = row
        assert tool_name == "search_knowledge"
        assert recorded_input == {"query": "frete"}
        assert success is True
        assert error is None
        assert latency_ms == 40
        assert owner == tenant
        assert output["chunks"][0]["content"] == FRETE

    async def test_a_failure_is_recorded_and_does_not_kill_the_turn(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """A tool that fails is an answer the agent has to work without — never
        an exception that costs the customer their reply."""
        thread = create_thread(admin, tenant)

        async with as_runtime_worker(dsn) as conn:
            result = await tools.run_tool(
                conn,
                SearchKnowledge(EmbeddingStandIn(fails=RuntimeError("HTTP 503 provider down"))),
                _context(tenant, thread.conversation_id),
                {"query": "frete"},
                clock=FrozenClock(START),
            )

        assert result.success is False
        (row,) = await _recorded(admin, thread.conversation_id)
        assert row[3] is False
        assert "503" in row[4]


class TestTheStandIn:
    """The stand-in is a test double; a double nobody checked is a lie."""

    async def test_it_embeds_the_text_it_was_given(self) -> None:
        result = await EmbeddingStandIn().embed(["frete"], model="whatever")

        assert isinstance(result, EmbeddingResult)
        assert result.vectors == (embed_text("frete"),)
        assert result.usage == Usage(input_tokens=1, output_tokens=None, cost_usd=0.0)

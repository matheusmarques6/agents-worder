"""The cost trail — one row per completed LLM call, written by `worker_role`.

The credential matters as much as the row: writing as a superuser would hide a
missing GRANT and a missing policy, and the first thing to break in production
would be a privilege this suite never exercised (decisão 21).

What is asserted here and nowhere else: that `internal.llm_calls` (§6.4)
accepts what the meter produces, that a call can be attributed to a conversation
or to nothing at all (an embedding has no conversation), and that a worker
scoped to tenant A cannot write a row that says tenant B spent the money.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
import pytest

from agents_runtime.repository import llm_calls as llm_repo
from tests.db.factories import create_tenant, create_thread


@asynccontextmanager
async def as_worker(dsn: str, tenant_id: uuid.UUID) -> AsyncIterator[psycopg.AsyncConnection]:
    """The runtime's own credential, scoped the way a unit of work scopes it."""
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await conn.execute("select set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        await conn.execute("set role worker_role")
        yield conn


@pytest.fixture
def tenant(admin: psycopg.Connection) -> uuid.UUID:
    tenant_id = create_tenant(admin)
    yield tenant_id
    with admin.cursor() as cur:
        cur.execute("delete from public.tenants where id = %s", (tenant_id,))


class TestTheRow:
    async def test_a_completed_call_is_the_row_the_cost_screen_reads(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        async with as_worker(dsn, tenant) as conn:
            call_id = await llm_repo.record_llm_call(
                conn,
                tenant_id=tenant,
                purpose="agent_reply",
                provider="openrouter",
                model="anthropic/claude-sonnet-5",
                input_tokens=120,
                output_tokens=40,
                cost_usd=0.00018,
                latency_ms=900,
            )
            await conn.commit()

        with admin.cursor() as cur:
            cur.execute(
                """
                select tenant_id, purpose, provider, model, input_tokens, output_tokens,
                       cost_usd, latency_ms, conversation_id
                  from internal.llm_calls where id = %s
                """,
                (call_id,),
            )
            row = cur.fetchone()

        assert row[0] == tenant
        assert row[1] == "agent_reply"
        # D1: both are recorded because the route is fixed and the model is not.
        assert row[2] == "openrouter"
        assert row[3] == "anthropic/claude-sonnet-5"
        assert (row[4], row[5]) == (120, 40)
        assert float(row[6]) == pytest.approx(0.00018)
        assert row[7] == 900
        assert row[8] is None

    async def test_a_reply_call_is_attributed_to_its_conversation(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """Without this link, "which conversation cost R$ 4 today" has no answer."""
        thread = create_thread(admin, tenant)

        async with as_worker(dsn, tenant) as conn:
            call_id = await llm_repo.record_llm_call(
                conn,
                tenant_id=tenant,
                purpose="agent_reply",
                provider="openrouter",
                model="anthropic/claude-sonnet-5",
                conversation_id=thread.conversation_id,
                input_tokens=10,
                output_tokens=5,
                cost_usd=0.00001,
                latency_ms=120,
            )
            await conn.commit()

        with admin.cursor() as cur:
            cur.execute("select conversation_id from internal.llm_calls where id = %s", (call_id,))
            (conversation_id,) = cur.fetchone()

        assert conversation_id == thread.conversation_id

    async def test_an_embedding_belongs_to_no_conversation(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """Ingesting the knowledge base (S6) happens outside any conversation —
        the trail still has to hold the spend."""
        async with as_worker(dsn, tenant) as conn:
            call_id = await llm_repo.record_llm_call(
                conn,
                tenant_id=tenant,
                purpose="embedding",
                provider="openrouter",
                model="openai/text-embedding-3-small",
                input_tokens=8,
                cost_usd=0.000002,
                latency_ms=40,
            )
            await conn.commit()

        with admin.cursor() as cur:
            cur.execute(
                "select purpose, output_tokens from internal.llm_calls where id = %s", (call_id,)
            )
            purpose, output_tokens = cur.fetchone()

        assert purpose == "embedding"
        assert output_tokens is None


class TestTheBoundary:
    async def test_a_worker_cannot_bill_another_tenant(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """The WITH CHECK half of the policy: a worker scoped to A writing a row
        that says B spent the money is how a cost screen starts lying."""
        stranger = create_tenant(admin)

        try:
            async with as_worker(dsn, tenant) as conn:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    await llm_repo.record_llm_call(
                        conn,
                        tenant_id=stranger,
                        purpose="agent_reply",
                        provider="openrouter",
                        model="anthropic/claude-sonnet-5",
                        input_tokens=1,
                        output_tokens=1,
                        cost_usd=0.1,
                        latency_ms=1,
                    )
        finally:
            with admin.cursor() as cur:
                cur.execute("delete from public.tenants where id = %s", (stranger,))

    def test_the_table_has_nowhere_to_put_content(self, admin: psycopg.Connection) -> None:
        """The PII law as a schema fact: cost, ids and timings — no prompt, no
        reply, no phone. A future column named `prompt` fails here first."""
        with admin.cursor() as cur:
            cur.execute(
                """
                select column_name from information_schema.columns
                 where table_schema = 'internal' and table_name = 'llm_calls'
                """
            )
            columns = {row[0] for row in cur.fetchall()}

        assert columns == {
            "id",
            "tenant_id",
            "purpose",
            "conversation_id",
            "eval_run_id",
            "provider",
            "model",
            "input_tokens",
            "output_tokens",
            "cost_usd",
            "latency_ms",
            "created_at",
        }

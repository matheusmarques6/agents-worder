"""The cost trail's SQL — `internal.llm_calls` (§6.4).

Like the rest of this layer, the function takes a connection and never opens or
commits one: the caller owns the transaction and the `SET LOCAL app.tenant_id`
scope that goes with it. That is what keeps the write a short transaction of
its own instead of something living across the LLM call that produced it.

The row is deliberately narrow. `provider` and `model` are both recorded
because D1 fixed the route (OpenRouter) and left the model as per-tenant
configuration — the trail has to say what was actually billed, not what the
platform assumes. There is no column for content, and there must never be: the
prompt and the reply live in `messages`, and the telemetry of S10 reads only
what is here.
"""

from uuid import UUID

import psycopg


async def record_llm_call(
    conn: psycopg.AsyncConnection,
    *,
    tenant_id: UUID,
    purpose: str,
    provider: str,
    model: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    latency_ms: int | None = None,
    conversation_id: UUID | None = None,
    eval_run_id: UUID | None = None,
) -> int:
    """One completed call. Returns the row's id."""
    cursor = await conn.execute(
        """
        insert into internal.llm_calls
            (tenant_id, purpose, conversation_id, eval_run_id, provider, model,
             input_tokens, output_tokens, cost_usd, latency_ms)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        returning id
        """,
        (
            tenant_id,
            purpose,
            conversation_id,
            eval_run_id,
            provider,
            model,
            input_tokens,
            output_tokens,
            cost_usd,
            latency_ms,
        ),
    )
    return (await cursor.fetchone())[0]

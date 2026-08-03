"""The tool trail's SQL — `internal.tool_calls` (§6.3).

Takes a connection, never opens one: the caller owns the short transaction and
the `SET LOCAL app.tenant_id` inside it.

`input` and `output` are jsonb and they DO hold content — a contact's question
and a customer's data. That is correct and deliberate: this table lives in
Postgres, where the content already is. The line that must never be crossed is
the one out of Postgres — the telemetry of S10 carries ids, latency and cost,
never these two columns.
"""

from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb


async def record_tool_call(
    conn: psycopg.AsyncConnection,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    tool_name: str,
    arguments: dict,
    output: dict | None,
    success: bool,
    error: str | None = None,
    latency_ms: int | None = None,
    message_id: UUID | None = None,
) -> int:
    """One execution — successful or not. Returns the row's id."""
    cursor = await conn.execute(
        """
        insert into internal.tool_calls
            (tenant_id, conversation_id, message_id, tool_name, input, output,
             success, error, latency_ms)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        returning id
        """,
        (
            tenant_id,
            conversation_id,
            message_id,
            tool_name,
            Jsonb(arguments),
            Jsonb(output) if output is not None else None,
            success,
            error,
            latency_ms,
        ),
    )
    return (await cursor.fetchone())[0]

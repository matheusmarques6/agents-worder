"""What a tool is, and the one way one is ever run.

A tool is where a message written by a stranger reaches the database. The model
chooses which one to call and with what arguments — after reading that message
— so a tool treats its arguments as hostile input and takes NOTHING about
authorisation from them. Scope comes from `ToolContext`, which comes from the
job, which came from the ingestion that resolved the tenant from the account.

`run_tool` is the only entry point, and it exists so three rules cannot be
forgotten by the next tool somebody writes:

  * **every execution is recorded** in `internal.tool_calls` — success and
    failure alike. "The agent tried to look it up and could not" is an answer a
    merchant is owed;
  * **a failing tool never kills the turn.** A tool failure is an answer the
    agent has to work without; letting it escape would cost the customer their
    reply over a provider being briefly down;
  * **latency comes from the injected clock**, like everywhere else.

Each tool opens its own SHORT transaction and sets `app.tenant_id` inside it
(ADR-6 holds within the responder too). Nothing here opens a transaction around
the tool itself, because that would be a transaction spanning whatever network
call the tool makes.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

import psycopg

from agents_runtime.clock import Clock
from agents_runtime.repository import tool_calls as tool_calls_repo
from agents_runtime.repository.scope import scope_to_tenant


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Where the tool is allowed to look. Never built from model output."""

    tenant_id: UUID
    conversation_id: UUID


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool: str
    success: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class Tool(Protocol):
    name: str

    async def __call__(
        self,
        conn: psycopg.AsyncConnection,
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> ToolResult: ...


def require_text(arguments: Mapping[str, Any], key: str) -> str:
    """Strict parsing, the webhook doctrine — never "use what parsed".

    Unknown keys are simply not read: the model may add whatever it likes, and
    a `tenant_id` among the arguments is ignored rather than honoured. What
    must NOT happen is coercion — `str(42)` would turn a wrong type into a
    plausible query.
    """
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key}: expected a non-empty string, got {type(value).__name__}")
    return value.strip()


async def run_tool(
    conn: psycopg.AsyncConnection,
    tool: Tool,
    context: ToolContext,
    arguments: Mapping[str, Any],
    *,
    clock: Clock,
) -> ToolResult:
    started = clock.now()
    try:
        result = await tool(conn, context, arguments)
    except Exception as error:
        # Expected failures are returned as `success=False` by the tool itself;
        # this catch is the net for the unexpected, and it records them the same
        # way instead of letting a bug reach the customer as silence.
        result = ToolResult(tool=tool.name, success=False, error=str(error))

    latency_ms = int((clock.now() - started).total_seconds() * 1000)

    async with conn.transaction():
        await scope_to_tenant(conn, context.tenant_id)
        await tool_calls_repo.record_tool_call(
            conn,
            tenant_id=context.tenant_id,
            conversation_id=context.conversation_id,
            tool_name=result.tool,
            arguments=dict(arguments),
            output=result.output,
            success=result.success,
            error=result.error,
            latency_ms=latency_ms,
        )

    return result

"""`get_customer_context` — who the agent is talking to.

Takes no arguments at all, and that is a security property rather than a
simplification: a tool that accepted a contact id would be a tool the model
could be talked into pointing somewhere else. The conversation is the one the
job is about, and the scope is the tenant the job belongs to.

The E2 answer is what exists today — name, language, opt status, since when,
how many conversations. The order-based context of RF-010 (`total_orders`,
`avg_ticket`, `first_order_at`) arrives with the order tables in E3; inventing
zeros for it now would be worse than its absence.
"""

from collections.abc import Mapping
from typing import Any

import psycopg

from agents_runtime.repository import contacts as contacts_repo
from agents_runtime.repository.scope import scope_to_tenant
from agents_runtime.tools.base import ToolContext, ToolResult


class GetCustomerContext:
    name = "get_customer_context"

    async def __call__(
        self,
        conn: psycopg.AsyncConnection,
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> ToolResult:
        async with conn.transaction():
            await scope_to_tenant(conn, context.tenant_id)
            facts = await contacts_repo.load_customer_facts(
                conn, conversation_id=context.conversation_id
            )

        if facts is None:
            # Not ours. Say so plainly instead of returning an empty customer,
            # which the model would read as "a customer we know nothing about".
            return ToolResult(
                tool=self.name,
                success=False,
                error="conversation not found for this tenant",
            )

        return ToolResult(
            tool=self.name,
            success=True,
            output={
                "name": facts.name,
                "language": facts.language,
                "opt_status": facts.opt_status,
                "customer_since": facts.first_seen_at.isoformat(),
                "last_message_at": (
                    facts.last_message_at.isoformat() if facts.last_message_at else None
                ),
                "conversations": facts.conversations,
            },
        )

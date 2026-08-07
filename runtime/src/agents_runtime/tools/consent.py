"""`record_optout` — the model detects the intention, the tool performs the act.

Via (c) of RF-033, and the division of labour is the whole design. Deciding
whether "para de me mandar mensagem, pelo amor de deus" is an opt-out is a
judgement about language, which is what a model is for. Deciding WHOSE contact
gets suppressed is authorisation, which a model never decides — the contact id
is looked up from the conversation the job is about, under the tenant's own
RLS, and an id among the model's arguments is ignored exactly like a
`tenant_id` would be.

Takes no arguments at all, for the same reason `get_customer_context` takes
none: a tool that accepted a contact is a tool a stranger's message could aim
somewhere else. `reason` is not an argument either — the model does not get to
say WHY it suppressed; the reason is `intent_optout`, always, because that is
the via this tool is.

**Opt-out suppresses sending; it does not erase data** (RNF-044). Nothing here
deletes a message, a conversation or a contact. The row that appears is the
record of the opposition, and it is what every proactive path already reads.
"""

from collections.abc import Mapping
from typing import Any

import psycopg

from agents_runtime.repository import consent as consent_repo
from agents_runtime.repository.scope import scope_to_tenant
from agents_runtime.tools.base import ToolContext, ToolResult

#: `suppression_list.reason` — the vocabulary the ladder maps to
#: `suppressed_optout`. Written once, here, next to the only writer of it.
REASON = "intent_optout"

#: `suppression_list.created_by` — the agent acted, no human did.
CREATED_BY = "agent"


class RecordOptout:
    name = "record_optout"

    async def __call__(
        self,
        conn: psycopg.AsyncConnection,
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> ToolResult:
        # Its OWN short transaction with `SET LOCAL` inside it (ADR-6 holds in
        # the responder too), and nothing around the model call.
        async with conn.transaction():
            await scope_to_tenant(conn, context.tenant_id)
            contact_id = await consent_repo.contact_of_conversation(
                conn, context.conversation_id
            )
            if contact_id is None:
                # Not ours. Said plainly rather than as an empty success: the
                # model must not read "done" where nothing happened.
                return ToolResult(
                    tool=self.name,
                    success=False,
                    error="conversation not found for this tenant",
                )

            recorded = await consent_repo.suppress_contact(
                conn, contact_id, reason=REASON, created_by=CREATED_BY
            )

        if recorded is None:
            return ToolResult(
                tool=self.name, success=False, error="contact not found for this tenant"
            )

        return ToolResult(
            tool=self.name,
            success=True,
            # `already` rather than a failure: the contact asked twice and the
            # answer is the same both times. A failure here would push the model
            # into apologising for having honoured the request.
            output={"suppressed": True, "already": not recorded, "reason": REASON},
        )

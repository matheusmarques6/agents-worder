"""`escalate_to_human` — the model recognises defeat, the tool arranges the exit.

Same division of labour as `record_optout`: judging that "quero falar com uma
pessoa de verdade" (or that the agent has run out of useful things to say) is a
judgement about language, and that is what a model is for. WHICH conversation
gets handed over is authorisation, and that never comes from the model — it is
the conversation the job is about.

Unlike `record_optout`, this one does take an argument, and the difference is
worth stating. Opt-out's reason is a controlled vocabulary that the ladder
branches on, so letting the model write it would let a sentence pick a code
path. Escalation's reason is a NOTE for the person picking the conversation up;
nothing branches on it, it is stored and shown. It is still hostile text, so it
is length-bounded — a model that emits a novel must not become a jsonb payload
nobody can open.

What the tool does NOT do is send anything. It moves the conversation to
`humano` and opens an alert; the reply to the customer is still the agent's, and
it still passes Judge 1.
"""

from collections.abc import Mapping
from typing import Any

import psycopg

from agents_runtime.agent_core.llm import ToolSpec
from agents_runtime.repository import alerts as alerts_repo
from agents_runtime.repository import handoff as handoff_repo
from agents_runtime.repository.scope import scope_to_tenant
from agents_runtime.tools.base import ToolContext, ToolResult, require_text

#: `alerts.type` — the row a human finds this by.
HANDOFF = "handoff"

#: A note, not a payload. Beyond this the text is cut: the field exists so a
#: person reads one line before opening the conversation.
REASON_LIMIT = 300


class EscalateToHuman:
    name = "escalate_to_human"
    spec = ToolSpec(
        name="escalate_to_human",
        description=(
            "Passa esta conversa para uma pessoa da loja. Use quando o cliente "
            "pedir para falar com um humano, ou quando o assunto sair do que "
            "você pode resolver (reembolso fora da política, reclamação grave, "
            "problema que exige decisão da loja)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "Uma linha para quem for assumir: o que o cliente quer e "
                        "por que precisa de uma pessoa."
                    ),
                }
            },
            "required": ["reason"],
            "additionalProperties": False,
        },
    )

    async def __call__(
        self,
        conn: psycopg.AsyncConnection,
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> ToolResult:
        # Parsed BEFORE the transaction opens: a handover with nothing written
        # on it is a conversation somebody has to read from the top, and a
        # refusal must leave the conversation exactly as it found it.
        reason = require_text(arguments, "reason")[:REASON_LIMIT]

        # Its own short transaction with `SET LOCAL` inside it — ADR-6 holds in
        # the responder too.
        async with conn.transaction():
            await scope_to_tenant(conn, context.tenant_id)
            moved = await handoff_repo.request_human(conn, conversation_id=context.conversation_id)
            if moved is None:
                return ToolResult(
                    tool=self.name,
                    success=False,
                    error="conversation not found for this tenant",
                )

            if moved:
                # Only the transition opens an alert. A row per attempt would
                # queue the same customer twice for the same person.
                await alerts_repo.open_alert(
                    conn,
                    tenant_id=context.tenant_id,
                    type=HANDOFF,
                    severity="warning",
                    title="O agente passou a conversa para uma pessoa",
                    payload={
                        "conversation_id": str(context.conversation_id),
                        "reason": reason,
                    },
                )

        return ToolResult(
            tool=self.name,
            success=True,
            # `already` rather than a failure, the shape `record_optout` settled:
            # the customer is waiting for a person either way, and a failure
            # would push the model into apologising for having arranged it.
            output={"escalated": True, "already": not moved},
        )

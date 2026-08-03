"""Judge 1's live scores — `internal.judge_scores` with `kind = 'pre_send'` (§6.2).

RNF-050: every reply records the Judge 1 score. Every ATTEMPT, in fact — the
regenerations are the audit trail of why a reply ended up looking the way it
does, and of why a blocked one never existed.

`message_id` stays NULL and that is not an omission: at pre-send time the
outbound message has not been written yet (it is created inside the FASE 3
transaction), and for a blocked draft it never will be. The conversation is the
link that always exists.

The eval trail writes to the same table from `repository/evals.py`, with
`kind = 'post_hoc'` and an `eval_run_id`. Two writers, two concerns: one scores
a version against a pack, this one decided whether a message left the outbox.
"""

from uuid import UUID

import psycopg

PRE_SEND = "pre_send"


async def record_pre_send_score(
    conn: psycopg.AsyncConnection,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    judge_model: str,
    score: float,
    verdict: str,
    rationale: str | None = None,
) -> int:
    cursor = await conn.execute(
        """
        insert into internal.judge_scores
            (tenant_id, kind, conversation_id, judge_model, score, verdict, rationale)
        values (%s, %s, %s, %s, %s, %s, %s)
        returning id
        """,
        (tenant_id, PRE_SEND, conversation_id, judge_model, score, verdict, rationale),
    )
    return (await cursor.fetchone())[0]

"""The post-hoc review's SQL — what a sent reply cost, and how a correction leaves.

Like the rest of this layer, every function takes a connection and never opens
or commits one: the caller owns the short transaction and the
`SET LOCAL app.tenant_id` scope that goes with it.

**The archaeology, and why it is by WINDOW.** The risk gate (decisão 92) reads
six signals, and three of them are not in the reply's text: the score Judge 1
gave, how many attempts it took, and whether the answer was built on retrieved
knowledge. Those rows exist — `judge_scores` and `tool_calls` — but they carry
no `message_id`: at pre-send time the outbound message has not been written yet
(it is created inside the FASE 3 transaction), and for a draft that never left
it never will be. What the rows do carry is a clock.

So a turn is the interval `(previous outbound message, this one]`, and the
signals of THIS reply are the rows that landed inside it. The alternative —
reading the conversation whole — would make one bad turn drag every later reply
into the expensive path, and the gate would stop measuring the reply.

Attributing by id instead would mean writing `message_id` back onto rows
created before the message existed: a second UPDATE inside the conclusion
transaction, which is the engine (Lei 1 of the E2 plan) and a cost paid by
every reply to serve the few that get audited.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

#: `internal.judge_scores.kind` — a score about a message that was SENT. The
#: pack's scores share the value from `repository/evals.py`; what separates them
#: is `eval_run_id` against `message_id`.
POST_HOC = "post_hoc"

#: The tool whose success means the reply asserted something from the
#: merchant's own knowledge base.
KNOWLEDGE_TOOL = "search_knowledge"


@dataclass(frozen=True, slots=True)
class SentReplyRow:
    """The reply the customer received, plus what the turn recorded about it."""

    tenant_id: UUID
    conversation_id: UUID
    text: str
    sent_at: datetime
    #: Where this turn began — the previous outbound message, or the start of
    #: the conversation when there is none.
    since: datetime | None
    judge_score: float | None
    judge_attempts: int
    used_knowledge: bool


@dataclass(frozen=True, slots=True)
class WindowMessage:
    author: str
    text: str


async def load_sent_reply(
    conn: psycopg.AsyncConnection, *, message_id: UUID
) -> SentReplyRow | None:
    """The sent reply and its turn's signals, or None when this connection
    cannot see such an outbound message — which is a bug, not an outcome."""
    cursor = await conn.execute(
        """
        with target as (
            select id, tenant_id, conversation_id, created_at,
                   content ->> 'text' as text
              from public.messages
             where id = %(message)s
               and direction = 'outbound'
        ),
        window_start as (
            select max(previous.created_at) as at
              from public.messages previous, target
             where previous.conversation_id = target.conversation_id
               and previous.direction = 'outbound'
               and previous.created_at < target.created_at
        )
        select target.tenant_id,
               target.conversation_id,
               target.text,
               target.created_at,
               window_start.at,
               (select count(*)
                  from internal.judge_scores score
                 where score.conversation_id = target.conversation_id
                   and score.kind = 'pre_send'
                   and score.created_at <= target.created_at
                   and (window_start.at is null or score.created_at > window_start.at)),
               (select score.score
                  from internal.judge_scores score
                 where score.conversation_id = target.conversation_id
                   and score.kind = 'pre_send'
                   and score.created_at <= target.created_at
                   and (window_start.at is null or score.created_at > window_start.at)
                 order by score.created_at desc, score.id desc
                 limit 1),
               exists (
                 select 1
                   from internal.tool_calls call
                  where call.conversation_id = target.conversation_id
                    and call.tool_name = %(tool)s
                    and call.success
                    and jsonb_array_length(coalesce(call.output -> 'chunks', '[]'::jsonb)) > 0
                    and call.created_at <= target.created_at
                    and (window_start.at is null or call.created_at > window_start.at)
               )
          from target, window_start
        """,
        {"message": message_id, "tool": KNOWLEDGE_TOOL},
    )
    row = await cursor.fetchone()
    if row is None:
        return None

    return SentReplyRow(
        tenant_id=row[0],
        conversation_id=row[1],
        text=row[2] or "",
        sent_at=row[3],
        since=row[4],
        # numeric(5,2) arrives as Decimal; the gate compares against a float.
        judge_score=float(row[6]) if row[6] is not None else None,
        judge_attempts=int(row[5]),
        used_knowledge=bool(row[7]),
    )


async def load_turn_window(
    conn: psycopg.AsyncConnection,
    *,
    conversation_id: UUID,
    since: datetime | None,
    until: datetime,
) -> tuple[WindowMessage, ...]:
    """What the contact said in the turn this reply answered.

    The judge needs it to tell an answer from a plausible sentence, and the
    think-gate's reading of the contact is re-derived from it — that gate is
    pure, so the same messages always give the same reason.
    """
    cursor = await conn.execute(
        """
        select author_type, content ->> 'text'
          from public.messages
         where conversation_id = %(conversation)s
           and direction = 'inbound'
           and created_at <= %(until)s
           and (%(since)s::timestamptz is null or created_at > %(since)s)
         order by created_at, seq
        """,
        {"conversation": conversation_id, "since": since, "until": until},
    )
    return tuple(WindowMessage(author=row[0], text=row[1] or "") for row in await cursor.fetchall())


async def post_hoc_score_exists(conn: psycopg.AsyncConnection, *, message_id: UUID) -> bool:
    """The done marker of a review. Written LAST, so a crash mid-review is
    replayed rather than silently dropped."""
    cursor = await conn.execute(
        """
        select exists (
          select 1 from internal.judge_scores
           where kind = %s and message_id = %s
        )
        """,
        (POST_HOC, message_id),
    )
    return bool((await cursor.fetchone())[0])


async def record_post_hoc_score(
    conn: psycopg.AsyncConnection,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    message_id: UUID,
    judge_model: str,
    score: float,
    verdict: str,
    rationale: str | None = None,
) -> int:
    """Unlike the pre-send score, this one HAS a message: it judges something
    that was said, so the row points at what was said."""
    cursor = await conn.execute(
        """
        insert into internal.judge_scores
            (tenant_id, kind, conversation_id, message_id, judge_model, score,
             verdict, rationale)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        returning id
        """,
        (
            tenant_id,
            POST_HOC,
            conversation_id,
            message_id,
            judge_model,
            score,
            verdict,
            rationale,
        ),
    )
    return (await cursor.fetchone())[0]


async def enqueue_correction(
    conn: psycopg.AsyncConnection, *, message_id: UUID, content: dict
) -> str:
    """`sent | already_sent | no_channel` — outcomes are data (S9b migration).

    A message id that does not exist raises inside the function instead: that
    is a bug in whoever enqueued the job, and the ladder to the DLQ is where a
    human sees it.
    """
    cursor = await conn.execute(
        "select internal.enqueue_correction(%s, %s)", (message_id, Jsonb(content))
    )
    return (await cursor.fetchone())[0]

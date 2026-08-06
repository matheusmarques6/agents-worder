"""The engine's SQL, in the one layer allowed to hold SQL.

Every statement the composition runs lives here — the fitness function
`test_no_sql_outside_repository` is what keeps it that way. Functions take a
connection rather than opening one: the composition owns connections and their
roles; this layer owns what is said over them.

Transactions are the caller's. `claim` and `conclude` must each be their own
short transaction with the tenant scope set inside it (`SET LOCAL` semantics),
so they take the connection mid-transaction and never commit.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from agents_runtime.channels.port import ClaimedSend
from agents_runtime.dispatch.ladder import ProactiveTouch
from agents_runtime.queueing.antiban import Pacing

# Re-exported so every caller from E1 keeps its import. The definition moved to
# `repository.scope` because this module imports `channels.port`, and modules
# that only needed the tenant scope were reaching the channel through it — the
# boundary contract caught it the day the real responder was born (S9).
from agents_runtime.repository.scope import scope_to_tenant  # noqa: F401

# --- the conversation turn ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimedConversation:
    last_processed_seq: int
    version: int


async def claim_conversation(
    conn: psycopg.AsyncConnection,
    conversation_id: UUID,
    token: UUID,
    *,
    lease: timedelta,
) -> ClaimedConversation | None:
    cursor = await conn.execute(
        "select * from internal.claim_conversation(%s, %s, %s)",
        (conversation_id, token, lease),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return ClaimedConversation(last_processed_seq=row[0], version=row[1])


async def release_lease(conn: psycopg.AsyncConnection, conversation_id: UUID, token: UUID) -> bool:
    cursor = await conn.execute("select internal.release_lease(%s, %s)", (conversation_id, token))
    return bool((await cursor.fetchone())[0])


async def renew_lease(
    conn: psycopg.AsyncConnection,
    conversation_id: UUID,
    token: UUID,
    *,
    lease: timedelta,
) -> bool:
    # The lease length always travels from config — the SQL default exists for
    # hand runs, and letting it win here once turned an immediate first beat
    # into a two-minute lease under a 200ms test config (cenário 5, flaky).
    cursor = await conn.execute(
        "select internal.renew_lease(%s, %s, %s)", (conversation_id, token, lease)
    )
    return bool((await cursor.fetchone())[0])


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    committed: bool
    outbound_seq: int | None
    outbox_id: UUID | None


async def conclude_turn(
    conn: psycopg.AsyncConnection,
    *,
    conversation_id: UUID,
    token: UUID,
    expected_version: int,
    generation: int,
    target_seq: int,
    content: dict[str, Any] | None,
    idempotency_key: str,
) -> TurnOutcome:
    """`content=None` means Judge 1 refused the draft: the turn concludes, the
    sequence advances and NOTHING goes out (S8, migration 20260803000003)."""
    cursor = await conn.execute(
        "select * from internal.conclude_turn(%s, %s, %s, %s, %s, %s, %s)",
        (
            conversation_id,
            token,
            expected_version,
            generation,
            target_seq,
            # SQL NULL, not `Jsonb(None)` — the latter renders the JSON value
            # `null`, which the function also refuses to send, but saying
            # "there is no content" plainly is what this call means.
            Jsonb(content) if content is not None else None,
            idempotency_key,
        ),
    )
    committed, outbound_seq, outbox_id = await cursor.fetchone()
    return TurnOutcome(committed=committed, outbound_seq=outbound_seq, outbox_id=outbox_id)


# --- the domain event: an abandonment becomes a funnel cadence ------------------


@dataclass(frozen=True, slots=True)
class DomainEventOutcome:
    """Outcomes are data (`applied`, `already_applied`, `discarded`,
    `invalid_payload`, `no_channel`, `no_funnel`) — the handler archives on all
    of them. A missing event raises instead: that is a bug, and bugs take the
    ladder.

    Since E3 S3, `applied` means "the funnel's cadence exists in
    `scheduled_touches`" and `outbox_id` is always None: no proactive touch
    reaches the outbox except through the dispatcher's ladder (D11)."""

    status: str
    conversation_id: UUID | None
    outbox_id: UUID | None


async def apply_domain_event(
    conn: psycopg.AsyncConnection, webhook_event_id: int
) -> DomainEventOutcome:
    cursor = await conn.execute(
        "select * from internal.apply_domain_event(%s)", (webhook_event_id,)
    )
    status, conversation_id, outbox_id = await cursor.fetchone()
    return DomainEventOutcome(status=status, conversation_id=conversation_id, outbox_id=outbox_id)


# --- the due proactive touch: claim, snapshot, compare-and-set ----------------


@dataclass(frozen=True, slots=True)
class ClaimedTouch:
    """One row of `internal.claim_due_touches` — ids only, on purpose.

    The tenant travels because the handler needs it BEFORE it can read anything
    (`SET LOCAL app.tenant_id`, ADR-11), exactly as `InboundJob` carries it. The
    facts do not travel: they are loaded under that scope, milliseconds before
    the ladder weighs them, and a copy inside a queue payload is a copy that can
    be minutes stale by the time it is read.
    """

    scheduled_touch_id: UUID
    tenant_id: UUID


async def claim_due_touches(
    conn: psycopg.AsyncConnection, worker_id: UUID, *, limit: int = 100
) -> list[ClaimedTouch]:
    cursor = await conn.execute(
        "select * from internal.claim_due_touches(%s, %s)", (worker_id, limit)
    )
    return [
        ClaimedTouch(scheduled_touch_id=row[0], tenant_id=row[1])
        for row in await cursor.fetchall()
    ]


@dataclass(frozen=True, slots=True)
class TouchSnapshot:
    """Everything one short transaction read about a due touch.

    `touch` is the ladder's input, built here rather than in the handler so the
    row and the rule meet in exactly one place; the rest is what the write needs
    afterwards. Nothing in this object is re-read before the compare-and-set —
    that is the whole point of D2, and the reason `touch.inbound_seq` matters.
    """

    status: str
    conversation_id: UUID | None
    touch_number: int
    cadence: list[Any]
    touch: ProactiveTouch
    #: `contacts.opt_status`, the projection of `suppression_list` (S6). Read
    #: here and not by the ladder because it decides what the touch SAYS, not
    #: whether it may exist — a contact who is merely `pending` is a contact we
    #: are allowed to write to and obliged to ask (RF-033a).
    opt_status: str | None = None

    #: `channels_accounts.type` of the number this touch would leave by (S7).
    #: Read here because anti-ban copy variation is only ever applied on
    #: Evolution — the Cloud sends an approved template, where varying the text
    #: would be varying something Meta approved as written.
    channel_type: str | None = None

    #: The text of the last proactive message this contact received, from the
    #: outbox — the one table every proactive origin passes through. It is what
    #: "the copy never repeats the last one" is measured against, and it is read
    #: in the SAME transaction as everything else so the comparison is against
    #: the world the decision saw.
    last_touch_text: str | None = None


async def load_touch_snapshot(
    conn: psycopg.AsyncConnection,
    touch_id: UUID,
    *,
    proactive_window: timedelta,
    funnel_cooldown: timedelta,
) -> TouchSnapshot | None:
    """The facts, as of now. `None` means the touch no longer exists.

    The two windows arrive as arguments, from `dispatch.ladder`, for the reason
    the S4 debt names: the module DECLARES the window and this query COUNTS it,
    so a literal here would be a second copy free to drift from the constant the
    rule is written against.
    """
    cursor = await conn.execute(
        """
        select t.status,
               t.conversation_id,
               t.touch_number,
               t.event_at,
               f.touches,
               (select s.reason
                  from public.suppression_list s
                 where s.tenant_id = t.tenant_id and s.contact_id = t.contact_id),
               (select max(m.created_at)
                  from public.messages m
                 where m.conversation_id = t.conversation_id
                   and m.direction = 'inbound'),
               coalesce((select o.financial_status = 'paid'
                           from public.orders o
                          where o.id = t.order_id), false),
               (select count(*)
                  from internal.message_outbox ob
                 where ob.contact_id = t.contact_id
                   and ob.kind in ('funnel_touch', 'followup')
                   and ob.created_at > now() - %s),
               tn.proactive_max_per_contact_24h,
               (select max(other.sent_at)
                  from public.scheduled_touches other
                 where other.contact_id = t.contact_id
                   and other.funnel_id <> t.funnel_id
                   and other.status = 'sent'
                   and other.sent_at > now() - %s),
               coalesce(c.next_inbound_seq, 0),
               ca.meta_tier,
               coalesce(ca.tier_usage_24h, 0),
               ct.opt_status,
               ca.type,
               (select ob.payload ->> 'text'
                  from internal.message_outbox ob
                 where ob.contact_id = t.contact_id
                   and ob.kind in ('funnel_touch', 'followup')
                 order by ob.created_at desc
                 limit 1)
          from public.scheduled_touches t
          join public.funnels f on f.id = t.funnel_id
          join public.tenants tn on tn.id = t.tenant_id
          left join public.contacts ct on ct.id = t.contact_id
          left join public.conversations c on c.id = t.conversation_id
          left join public.channels_accounts ca on ca.id = c.channel_account_id
         where t.id = %s
        """,
        (proactive_window, funnel_cooldown, touch_id),
    )
    row = await cursor.fetchone()
    if row is None:
        return None

    return TouchSnapshot(
        status=row[0],
        conversation_id=row[1],
        touch_number=row[2],
        cadence=list(row[4] or []),
        touch=ProactiveTouch(
            event_at=row[3],
            inbound_seq=row[11],
            suppression_reason=row[5],
            last_inbound_at=row[6],
            order_paid=row[7],
            proactive_sends_last_24h=row[8],
            max_proactive_per_24h=row[9],
            other_funnel_touch_at=row[10],
            tier_limit_24h=row[12],
            tier_usage_24h=row[13],
        ),
        opt_status=row[14],
        channel_type=row[15],
        last_touch_text=row[16],
    )


async def open_copy_violation_alert(
    conn: psycopg.AsyncConnection,
    tenant_id: UUID,
    touch_id: UUID,
    violations: tuple[str, ...],
) -> bool:
    """D3b: the deterministic gate stopped a variation, so a human is told.

    One alert per touch, not per attempt — the job climbs the retry ladder and
    three identical alerts for one touch would turn the signal into noise in the
    exact place somebody has to react.
    """
    cursor = await conn.execute(
        "select internal.open_copy_violation_alert(%s, %s, %s)",
        (tenant_id, touch_id, list(violations)),
    )
    row = await cursor.fetchone()
    return bool(row and row[0])


@dataclass(frozen=True, slots=True)
class TouchDispatch:
    """`sent` wrote everything; `conflict` wrote NOTHING and a fact moved;
    `gone` means somebody else already finished this touch; `no_channel` means
    the touch has no way out and a human has to look."""

    status: str
    outbox_id: UUID | None


async def dispatch_touch(
    conn: psycopg.AsyncConnection,
    touch_id: UUID,
    *,
    inbound_seq: int,
    max_proactive_per_24h: int,
    proactive_window: timedelta,
    funnel_cooldown: timedelta,
    tier_pause_fraction: float,
    payload: dict[str, Any],
    idempotency_key: str,
) -> TouchDispatch:
    """The compare-and-set of D2 — the ladder's guards, as a `WHERE`.

    The guard values are the ones the DECISION used, never re-read here: a
    revalidation that fetched its own thresholds could be laxer than the rule
    that allowed the touch, which is the one direction a protection may never
    move.
    """
    cursor = await conn.execute(
        "select * from internal.dispatch_touch(%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            touch_id,
            inbound_seq,
            max_proactive_per_24h,
            proactive_window,
            funnel_cooldown,
            tier_pause_fraction,
            Jsonb(payload),
            idempotency_key,
        ),
    )
    status, outbox_id = await cursor.fetchone()
    return TouchDispatch(status=status, outbox_id=outbox_id)


async def cancel_touch(conn: psycopg.AsyncConnection, touch_id: UUID, reason: str) -> bool:
    cursor = await conn.execute("select internal.cancel_touch(%s, %s)", (touch_id, reason))
    return bool((await cursor.fetchone())[0])


# --- the coalescer -----------------------------------------------------------


async def coalesce_due_conversations(
    conn: psycopg.AsyncConnection, *, queue: str, limit: int = 100
) -> int:
    """Returns how many jobs were created — the tick's only observable."""
    cursor = await conn.execute(
        "select count(*) from internal.coalesce_due_conversations(%s, %s)", (queue, limit)
    )
    return int((await cursor.fetchone())[0])


# --- the outbox --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimedRow:
    """One claimed outbox row: what to send, and the rhythm the number may send at.

    Two objects rather than one flat record, because the boundary is real: the
    channel adapter receives `send` and nothing else. An adapter that could see
    `Pacing` would be an adapter that could decide to ignore it — and delivery
    rhythm is the sender's, never the provider's (D10).
    """

    send: ClaimedSend
    pacing: Pacing
    channel_account_id: UUID
    """Which number this leaves by. Not on `Pacing` because the rule does not
    need it — a pure gate weighs facts, it does not address rows — and not on
    `ClaimedSend` because the adapter has no business with our account ids."""


async def claim_outbox_batch(
    conn: psycopg.AsyncConnection,
    token: UUID,
    *,
    lease: timedelta,
    limit: int = 50,
) -> list[ClaimedRow]:
    cursor = await conn.execute(
        "select * from internal.claim_outbox_batch(%s, %s, %s)", (token, limit, lease)
    )
    return [
        ClaimedRow(
            send=ClaimedSend(
                outbox_id=row[0],
                tenant_id=row[1],
                channel_type=row[2],
                channel_external_id=row[3],
                to_phone_e164=row[4],
                payload=row[5],
                idempotency_key=row[6],
                attempt_count=row[7],
            ),
            pacing=Pacing(
                channel_type=row[2],
                proactive=row[8],
                risk_accepted=row[10],
                warmup_stage=row[11],
                daily_cap=row[12],
                sends_today=row[13],
                next_send_at=row[14],
            ),
            channel_account_id=row[9],
        )
        for row in await cursor.fetchall()
    ]


async def defer_outbox_send(
    conn: psycopg.AsyncConnection, outbox_id: UUID, token: UUID, *, retry_in: timedelta
) -> bool:
    """Put the row back without spending an attempt — waiting is not failing."""
    cursor = await conn.execute(
        "select internal.defer_outbox_send(%s, %s, %s)", (outbox_id, token, retry_in)
    )
    return bool((await cursor.fetchone())[0])


async def record_channel_send(
    conn: psycopg.AsyncConnection,
    channel_account_id: UUID,
    *,
    proactive: bool,
    next_send_at: datetime | None,
    tier_pause_fraction: float,
) -> None:
    """Count what left, so the ceilings and the Meta tier know about it.

    `tier_pause_fraction` travels from `dispatch.ladder`, the same constant the
    ladder BLOCKS with. A copy of 0.8 in SQL would be the day the alert fires at
    one threshold and the pause happens at another.
    """
    await conn.execute(
        "select internal.record_channel_send(%s, %s, %s, %s)",
        (channel_account_id, proactive, next_send_at, tier_pause_fraction),
    )


async def mark_outbox_sent(
    conn: psycopg.AsyncConnection, outbox_id: UUID, token: UUID, provider_message_id: str
) -> bool:
    cursor = await conn.execute(
        "select internal.mark_outbox_sent(%s, %s, %s)",
        (outbox_id, token, provider_message_id),
    )
    return bool((await cursor.fetchone())[0])


async def mark_outbox_failed(
    conn: psycopg.AsyncConnection,
    outbox_id: UUID,
    token: UUID,
    *,
    transient: bool,
    error: str,
    retry_in: timedelta,
) -> bool:
    cursor = await conn.execute(
        "select internal.mark_outbox_failed(%s, %s, %s, %s, %s)",
        (outbox_id, token, transient, error, retry_in),
    )
    return bool((await cursor.fetchone())[0])


async def sweep_outbox_unknown(conn: psycopg.AsyncConnection) -> int:
    cursor = await conn.execute("select internal.sweep_outbox_unknown()")
    return int((await cursor.fetchone())[0])


async def review_stale_unknown(conn: psycopg.AsyncConnection, *, review_after: timedelta) -> int:
    cursor = await conn.execute("select internal.review_stale_unknown(%s)", (review_after,))
    return int((await cursor.fetchone())[0])


async def reprocess_dead_letters(
    conn: psycopg.AsyncConnection, dead_letter_queue: str, origin_queue: str
) -> int:
    cursor = await conn.execute(
        "select internal.reprocess_dead_letters(%s, %s)",
        (dead_letter_queue, origin_queue),
    )
    return int((await cursor.fetchone())[0])


# --- liveness ----------------------------------------------------------------


async def beat(conn: psycopg.AsyncConnection, process_name: str) -> None:
    await conn.execute(
        """
        insert into internal.runtime_heartbeats (process_name)
        values (%s)
        on conflict (process_name) do update set beat_at = now()
        """,
        (process_name,),
    )


# --- queue plumbing shared with the loop --------------------------------------


async def set_visibility(
    conn: psycopg.AsyncConnection, queue: str, message_id: int, delay: timedelta
) -> None:
    # pgmq speaks whole seconds. Truncating would turn any sub-second delay
    # into ZERO — a message visible again immediately, which under a tiny test
    # VT showed up as read_ct = 11 on a single job. Rounding UP keeps 'hidden
    # for at least this long' true at every granularity.
    await conn.execute(
        "select pgmq.set_vt(%s, %s, %s::integer)",
        (queue, message_id, max(1, math.ceil(delay.total_seconds()))),
    )


async def send_to_queue(conn: psycopg.AsyncConnection, queue: str, payload: dict[str, Any]) -> int:
    cursor = await conn.execute("select pgmq.send(%s, %s)", (queue, Jsonb(payload)))
    return int((await cursor.fetchone())[0])

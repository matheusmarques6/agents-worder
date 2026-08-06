"""Test data for the E3 slice — funnels, touches, the order mirror and the trail.

A separate module from `factories.py` on purpose: the E3 step may not edit what
earlier milestones wrote, and a new file is cheaper than a merge conflict. The
shape is the same — one row per call, a connection taken rather than opened, so
a test can arrange as the superuser and then attack as `worker_role`.
"""

import uuid
from dataclasses import dataclass

import psycopg

from tests.db.factories import unique_id, unique_phone


@dataclass(frozen=True)
class Funnel:
    id: uuid.UUID
    occasion: str


def create_customer(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    connector_account_id: uuid.UUID,
    *,
    external_id: str | None = None,
    phone_e164: str | None = None,
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into public.customers
                (tenant_id, connector_account_id, external_id, name, email, phone_e164)
            values (%s, %s, %s, 'Cliente', 'cliente@example.test', %s)
            returning id
            """,
            (
                tenant_id,
                connector_account_id,
                external_id or unique_id("cust"),
                phone_e164 or unique_phone(),
            ),
        )
        (customer_id,) = cur.fetchone()
    return customer_id


def create_order(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    connector_account_id: uuid.UUID,
    *,
    external_id: str | None = None,
    financial_status: str = "pending",
    total: str = "199.90",
    currency: str = "BRL",
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into public.orders
                (tenant_id, connector_account_id, external_id, financial_status,
                 total, currency, items)
            values (%s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (
                tenant_id,
                connector_account_id,
                external_id or unique_id("order"),
                financial_status,
                total,
                currency,
                psycopg.types.json.Jsonb([{"sku": "A1", "qty": 1}]),
            ),
        )
        (order_id,) = cur.fetchone()
    return order_id


#: The default cadence — kept exactly as S2 wrote it inline, so a test that
#: does not care about timing gets the same two touches it always got.
DEFAULT_TOUCHES = [
    {"n": 1, "delay": "PT0S", "copy_base": "Vi que você deixou o carrinho."},
    {"n": 2, "delay": "PT24H", "copy_base": "Ainda dá tempo de concluir."},
]


def create_funnel(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    *,
    occasion: str = "checkout_abandoned",
    enabled: bool = True,
    channel_preference: str = "auto",
    max_touches: int = 4,
    touches: list[dict] | None = None,
) -> Funnel:
    """`touches` is additive (S3): S2's callers keep the cadence they had, and a
    test that reasons about `due_at` states its own."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into public.funnels
                (tenant_id, occasion, enabled, channel_preference, touches, max_touches)
            values (%s, %s, %s, %s, %s, %s)
            returning id
            """,
            (
                tenant_id,
                occasion,
                enabled,
                channel_preference,
                psycopg.types.json.Jsonb(DEFAULT_TOUCHES if touches is None else touches),
                max_touches,
            ),
        )
        (funnel_id,) = cur.fetchone()
    return Funnel(id=funnel_id, occasion=occasion)


def create_suppression(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    *,
    reason: str = "explicit_block",
    created_by: str = "system",
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into public.suppression_list (tenant_id, contact_id, reason, created_by)
            values (%s, %s, %s, %s)
            returning id
            """,
            (tenant_id, contact_id, reason, created_by),
        )
        (row_id,) = cur.fetchone()
    return row_id


def create_scheduled_touch(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    funnel_id: uuid.UUID,
    contact_id: uuid.UUID,
    *,
    conversation_id: uuid.UUID | None = None,
    order_id: uuid.UUID | None = None,
    touch_number: int = 1,
    due_in_seconds: int = 0,
    event_age_seconds: int = 60,
    status: str = "pending",
    cancel_reason: str | None = None,
    sent_ago_seconds: int | None = None,
    outbox_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """One scheduled touch. Times are relative so a test never writes a literal.

    `event_age_seconds` is how long ago the fact that justifies the touch
    happened — the instant staleness is measured against, not the due date.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into public.scheduled_touches
                (tenant_id, funnel_id, contact_id, conversation_id, order_id,
                 touch_number, event_at, due_at, status, cancel_reason, sent_at, outbox_id)
            values (%s, %s, %s, %s, %s, %s,
                    now() - make_interval(secs => %s),
                    now() + make_interval(secs => %s),
                    %s, %s,
                    case when %s::integer is null then null
                         else now() - make_interval(secs => %s::integer) end,
                    %s)
            returning id
            """,
            (
                tenant_id,
                funnel_id,
                contact_id,
                conversation_id,
                order_id,
                touch_number,
                event_age_seconds,
                due_in_seconds,
                status,
                cancel_reason,
                sent_ago_seconds,
                sent_ago_seconds,
                outbox_id,
            ),
        )
        (touch_id,) = cur.fetchone()
    return touch_id


def create_funnel_conversion(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    *,
    funnel_id: uuid.UUID | None = None,
    contact_id: uuid.UUID | None = None,
    touch_id: uuid.UUID | None = None,
    order_id: uuid.UUID | None = None,
    amount: str = "199.90",
    currency: str = "BRL",
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into public.funnel_conversions
                (tenant_id, funnel_id, contact_id, scheduled_touch_id, order_id,
                 amount, currency)
            values (%s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (tenant_id, funnel_id, contact_id, touch_id, order_id, amount, currency),
        )
        (conversion_id,) = cur.fetchone()
    return conversion_id


def create_audit_entry(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID | None,
    *,
    action: str = "agent.approve",
    actor_type: str = "system",
    actor_user_id: uuid.UUID | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into public.audit_log (tenant_id, actor_type, actor_user_id, action, payload)
            values (%s, %s, %s, %s, %s)
            returning id
            """,
            (
                tenant_id,
                actor_type,
                actor_user_id,
                action,
                psycopg.types.json.Jsonb({"note": "fixture"}),
            ),
        )
        (entry_id,) = cur.fetchone()
    return entry_id

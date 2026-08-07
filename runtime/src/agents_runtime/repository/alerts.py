"""Alerts the merchant sees — `public.alerts` (§6.5).

Takes a connection, never opens one: the caller owns the short transaction and
the `SET LOCAL app.tenant_id` inside it.

The first writer is Judge 1 (S8), and the reason is worth stating: a reply that
is blocked leaves the customer with silence, and silence is indistinguishable
from a broken agent unless somebody is told. The alert is what turns a
deliberate non-send into an event a human can look at.
"""

from datetime import timedelta
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

#: The alert Judge 1 opens when a draft never left (`alerts.type` CHECK).
CRITICAL_VIOLATION = "critical_violation"


async def open_alert(
    conn: psycopg.AsyncConnection,
    *,
    tenant_id: UUID,
    type: str,
    severity: str,
    title: str,
    payload: dict | None = None,
) -> UUID:
    cursor = await conn.execute(
        """
        insert into public.alerts (tenant_id, type, severity, title, payload)
        values (%s, %s, %s, %s, %s)
        returning id
        """,
        (tenant_id, type, severity, title, Jsonb(payload if payload is not None else {})),
    )
    return (await cursor.fetchone())[0]


async def sweep_health_alerts(
    conn: psycopg.AsyncConnection,
    *,
    touch_stuck_after: timedelta,
    sync_error_after: timedelta,
) -> int:
    """The three silent failures of E3 become rows. Returns how many opened.

    Cross-tenant, so the SQL is `SECURITY DEFINER` with no filter parameter —
    the mould of every claim in this codebase. Both deadlines travel as
    parameters because they live in `QueueingConfig`, and a second copy of a
    number is a divergence waiting for the day somebody changes one of them.

    It observes and counts. It repairs nothing, and in particular it never puts
    a stuck touch back to `pending`: that would be a second clock, and a second
    clock can resend what already went out.
    """
    cursor = await conn.execute(
        "select internal.sweep_health_alerts(%s, %s)",
        (touch_stuck_after, sync_error_after),
    )
    return int((await cursor.fetchone())[0])

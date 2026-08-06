"""The order mirror, read from the contact's side (§3.3, §3.4).

Takes a connection and opens no transaction: the caller owns the short one and
the `SET LOCAL app.tenant_id` inside it, exactly like every other repository
module here.

There is no `where tenant_id = …` — the policy scopes, never a hand-written
clause (CLAUDE.md, trust boundaries). But RLS is only HALF of what a tool that
reads orders needs: inside one tenant every contact would still see every other
contact's orders, and a contact who can name an order number is a contact who
can enumerate them. So the query starts at the CONTACT and walks to the orders,
never the other way round:

    contacts.customer_id → customers → orders.customer_external_id

`orders.customer_external_id` is text and deliberately not an FK (the order
event usually arrives before the customer is mirrored), so the join is
`connector_account_id` AND `customer_external_id` together — the pair the
mirror's own uniqueness is built on. Joining on the external id alone would pair
a contact with somebody else's order in another store.

A contact whose `customer_id` is still NULL simply has no orders here. That is
an answer, not a failure: the mirror is filled by events, and "we have not
linked you to a store customer yet" is the truth at that moment.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import psycopg


@dataclass(frozen=True, slots=True)
class MirroredOrder:
    """One `orders` row, by value.

    `total` stays a `Decimal` here and is rendered as text by the tool: the
    output of a tool is stored as jsonb and read by a model, and 189.90 through
    a float becomes 189.89999999999998 in both places.
    """

    id: UUID
    external_id: str
    status: str | None
    financial_status: str
    total: Decimal | None
    currency: str
    items: list
    tracking_code: str | None
    tracking_status: str | None
    platform_created_at: datetime | None


_COLUMNS = """
    order_row.id, order_row.external_id, order_row.status, order_row.financial_status,
    order_row.total, order_row.currency, order_row.items,
    order_row.tracking_code, order_row.tracking_status, order_row.platform_created_at
"""


async def load_order(
    conn: psycopg.AsyncConnection,
    *,
    contact_id: UUID,
    external_id: str | None = None,
) -> MirroredOrder | None:
    """This contact's order — the named one, or the most recent when unnamed.

    One query for both questions on purpose: two would be two places where the
    walk from the contact could be forgotten, and forgetting it in ONE of them
    is the whole vulnerability.
    """
    cursor = await conn.execute(
        f"""
        select {_COLUMNS}
          from public.contacts contact
          join public.customers customer on customer.id = contact.customer_id
          join public.orders order_row
            on order_row.connector_account_id = customer.connector_account_id
           and order_row.customer_external_id = customer.external_id
         where contact.id = %(contact_id)s
           and (%(external_id)s::text is null or order_row.external_id = %(external_id)s)
         -- The mirror is filled by events, so `created_at` is when WE heard,
         -- not when the customer ordered. The platform's own clock decides
         -- which order is "the last one" whenever it told us.
         order by coalesce(order_row.platform_created_at, order_row.created_at) desc,
                  order_row.id desc
         limit 1
        """,
        {"contact_id": contact_id, "external_id": external_id},
    )
    row = await cursor.fetchone()
    if row is None:
        return None

    return MirroredOrder(
        id=row[0],
        external_id=row[1],
        status=row[2],
        financial_status=row[3],
        total=row[4],
        currency=row[5],
        items=row[6],
        tracking_code=row[7],
        tracking_status=row[8],
        platform_created_at=row[9],
    )

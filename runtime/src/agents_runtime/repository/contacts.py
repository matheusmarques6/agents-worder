"""What is known about the person on the other side (§4 `contacts`).

Scoped by the policy, not by a WHERE: the conversation id is the one thing a
tool could be pointed at, and a stranger's conversation simply does not exist
for a connection scoped to another tenant. That is the whole guard, and
`tests/db/test_tools.py` is what watches it.

The orders arrived (E3 S9). `total_orders`, `avg_ticket` and `first_order_at` —
the fields RF-010 injects into the prompt — come from `customers`, reached
through `contacts.customer_id`, and the join is a LEFT one because the
distinction decisão 81b insisted on is exactly what it produces:

  * `customer` is None — this contact has never been linked to a store customer.
    NO RECORD. The prompt gets no `customer_context` layer at all, because
    inventing one would be inventing data;
  * `customer` is present with `total_orders = 0` — NO HISTORY. A first-time
    buyer is a fact worth stating, or the model assumes the history failed to
    load.

Returning a zero for the first case is the bug this shape exists to prevent.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import psycopg


@dataclass(frozen=True, slots=True)
class OrderHistory:
    """The mirrored `customers` row, by value. Absent means never linked."""

    total_orders: int
    #: `Decimal`, rendered as text by whoever shows it: 189.90 through a float
    #: is 189.89999999999998 in a prompt and in jsonb alike.
    avg_ticket: Decimal | None
    first_order_at: datetime | None


@dataclass(frozen=True, slots=True)
class CustomerFacts:
    contact_id: UUID
    name: str | None
    language: str | None
    opt_status: str
    first_seen_at: datetime
    last_message_at: datetime | None
    #: How many conversations this contact has had with this store — the cheap
    #: answer to "is this someone we already know?".
    conversations: int
    #: None = never linked to a store customer, which is NOT the same as a
    #: customer who has bought nothing.
    orders: OrderHistory | None = None


async def load_customer_facts(
    conn: psycopg.AsyncConnection, *, conversation_id: UUID
) -> CustomerFacts | None:
    """The contact behind a conversation, or None when it is not ours."""
    cursor = await conn.execute(
        """
        select contact.id, contact.name, contact.language, contact.opt_status,
               contact.first_seen_at, contact.last_message_at,
               (select count(*) from public.conversations other
                 where other.contact_id = contact.id),
               customer.id, customer.total_orders, customer.avg_ticket,
               customer.first_order_at
          from public.conversations conversation
          join public.contacts contact on contact.id = conversation.contact_id
          -- LEFT, and that is the whole point: an unlinked contact must come
          -- back as "no record", never as a customer who bought nothing.
          left join public.customers customer on customer.id = contact.customer_id
         where conversation.id = %s
        """,
        (conversation_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None

    return CustomerFacts(
        contact_id=row[0],
        name=row[1],
        language=row[2],
        opt_status=row[3],
        first_seen_at=row[4],
        last_message_at=row[5],
        conversations=row[6],
        orders=(
            None
            if row[7] is None
            else OrderHistory(total_orders=row[8], avg_ticket=row[9], first_order_at=row[10])
        ),
    )

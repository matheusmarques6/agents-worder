"""What is known about the person on the other side (§4 `contacts`).

Scoped by the policy, not by a WHERE: the conversation id is the one thing a
tool could be pointed at, and a stranger's conversation simply does not exist
for a connection scoped to another tenant. That is the whole guard, and
`tests/db/test_tools.py` is what watches it.

What is NOT here is as important as what is: no orders. `total_orders`,
`avg_ticket` and `first_order_at` — the fields RF-010 injects into the prompt —
come from the store mirror, and the order tables arrive in E3. Returning a zero
for them today would be inventing data, and the prompt already distinguishes
"no record" from "no history" (decisão 81b).
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg


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


async def load_customer_facts(
    conn: psycopg.AsyncConnection, *, conversation_id: UUID
) -> CustomerFacts | None:
    """The contact behind a conversation, or None when it is not ours."""
    cursor = await conn.execute(
        """
        select contact.id, contact.name, contact.language, contact.opt_status,
               contact.first_seen_at, contact.last_message_at,
               (select count(*) from public.conversations other
                 where other.contact_id = contact.id)
          from public.conversations conversation
          join public.contacts contact on contact.id = conversation.contact_id
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
    )

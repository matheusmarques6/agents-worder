"""Consent and its opposite, as rows — the three writers of `suppression_list`.

`suppression_list` is the authority on "may we message this contact?", and
`contacts.opt_status` is its projection (S6; `core/dicionario-de-dados.md`
§4.1/§4.4). Nothing here writes `opt_status` for a block: the trigger the
migration installs does, so that every writer — this module, the operator's
path in E5, an admin script — gets the same projection without having to
remember it.

Three functions, one per via of RF-033, plus the read the button path needs:

  * `suppress_contact` — vias (a) explicit block and (c) intent opt-out. Short
    transaction, `SET LOCAL app.tenant_id`, and the tenant resolved from the
    contact row under RLS — never from an argument;
  * `authorize_contact` — the other half of via (a). It never removes a
    suppression: a tap cannot undo a record of somebody asking not to be
    messaged, and the one direction consent may move without ceremony is the
    strict one;
  * `suppress_silent_contacts` — via (b), cross-tenant, in the mould of
    `claim_due_touches`: a sweep with no filter parameter, because "only tenant
    X" would be an arbitrary cross-tenant query under another name (ADR-11).
"""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import psycopg


async def suppress_contact(
    conn: psycopg.AsyncConnection,
    contact_id: UUID,
    *,
    reason: str,
    created_by: str,
) -> bool | None:
    """`True` newly suppressed · `False` already was · `None` not this tenant's."""
    cursor = await conn.execute(
        "select internal.suppress_contact(%s, %s, %s)", (contact_id, reason, created_by)
    )
    return (await cursor.fetchone())[0]


async def authorize_contact(conn: psycopg.AsyncConnection, contact_id: UUID) -> bool | None:
    """`True` authorised · `False` refused (a suppression exists) · `None` not ours."""
    cursor = await conn.execute("select internal.authorize_contact(%s)", (contact_id,))
    return (await cursor.fetchone())[0]


async def suppress_silent_contacts(
    conn: psycopg.AsyncConnection, *, distinct_funnels: int
) -> int:
    """How many contacts the sweep removed this pass.

    The threshold travels as a parameter for the same reason the ladder's
    windows do: the number RF-033 states lives in `dispatch/consent.py`, and a
    literal in the SQL would be a second copy free to drift from it.
    """
    cursor = await conn.execute(
        "select internal.suppress_silent_contacts(%s)", (distinct_funnels,)
    )
    return int((await cursor.fetchone())[0])


async def load_inbound_contents(
    conn: psycopg.AsyncConnection,
    *,
    conversation_id: UUID,
    after_seq: int,
    target_seq: int,
) -> Sequence[dict[str, Any]]:
    """The raw content of the inbound messages this turn is about, in order.

    Raw, not the transcript's text: a button reply has no text worth reading,
    and what identifies it is the id WE issued, which lives in the jsonb the
    ingestion stored. The turn's own window (`after_seq`, `target_seq`) is used
    rather than "everything recent" so a redelivered job cannot re-read a
    button the previous turn already answered.
    """
    cursor = await conn.execute(
        """
        select content
          from public.messages
         where conversation_id = %s
           and direction = 'inbound'
           and seq > %s
           and seq <= %s
         order by seq
        """,
        (conversation_id, after_seq, target_seq),
    )
    return [row[0] for row in await cursor.fetchall()]


async def contact_of_conversation(
    conn: psycopg.AsyncConnection, conversation_id: UUID
) -> UUID | None:
    """Whose conversation this is — `None` when it is not this tenant's.

    The one lookup the button path needs, and it is a lookup rather than a
    payload field on purpose: the job carries ids the coalescer produced, and
    the contact behind a conversation is a fact the database owns.
    """
    cursor = await conn.execute(
        "select contact_id from public.conversations where id = %s", (conversation_id,)
    )
    row = await cursor.fetchone()
    return row[0] if row else None

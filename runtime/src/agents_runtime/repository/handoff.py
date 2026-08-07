"""Handing a conversation to a person — `conversations.state = 'humano'`.

The state already exists (migration 0003) and so does the vocabulary; what was
missing was anybody able to enter it. `takeover_user_id`/`takeover_at` stay NULL
on purpose: those record that a HUMAN took the conversation, and at this point
none has — the conversation is waiting, not attended. Writing them here would
make the E5 inbox unable to tell "queued for a person" from "somebody is on it".

Takes a connection, opens no transaction: the caller owns the short one.
"""

from uuid import UUID

import psycopg


async def request_human(conn: psycopg.AsyncConnection, *, conversation_id: UUID) -> bool | None:
    """True when this call moved the conversation, False when it already was
    waiting, None when the conversation is not this connection's to move.

    Three answers rather than two because they are three different facts, and
    collapsing "already waiting" into "done" would have the agent apologise for
    a handover it made a minute ago.

    The UPDATE is conditional on the state, so two turns racing to escalate the
    same conversation produce one winner and one "already" — the decision is the
    database's, not a read-then-write in Python.
    """
    cursor = await conn.execute(
        """
        update public.conversations
           set state = 'humano'
         where id = %s
           and state = 'ia'
        returning id
        """,
        (conversation_id,),
    )
    if await cursor.fetchone() is not None:
        return True

    # Nothing moved. Either it was already waiting for a person (or closed), or
    # the row is not visible to this connection at all — and those are opposite
    # answers, so the distinction is a second read rather than a guess.
    cursor = await conn.execute(
        "select state from public.conversations where id = %s", (conversation_id,)
    )
    row = await cursor.fetchone()
    return None if row is None else False

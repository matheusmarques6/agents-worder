"""pgmq as the runtime touches it — the only place queue SQL is allowed to live.

Three operations, and every read has to end in one of the terminal ones: a
message that is read and neither archived nor made visible again is the limbo
state the whole queue discipline exists to prevent (ADR-06). Backoff, DLQ and
weighted polling are the shape of that discipline and arrive in E1; what is
here is the transport they will be built on.

Every statement runs on an autocommit connection on purpose: a queue operation
is its own unit of work, and no transaction may stay open across the work the
job triggers.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


@dataclass(frozen=True, slots=True)
class QueueMessage:
    """A job claimed from a queue: its identity, and what it asks for."""

    id: int
    payload: dict[str, Any]
    # How many times pgmq has handed this message out. Attempt one is the
    # first read; the retry rules of unidade 4 are driven by this number.
    read_count: int = 1


class PgmqQueue:
    """One pgmq queue, over one connection."""

    def __init__(self, connection: psycopg.AsyncConnection, name: str) -> None:
        self._connection = connection
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def connection(self) -> psycopg.AsyncConnection:
        """For the loop's set_vt/DLQ plumbing — same connection, same discipline."""
        return self._connection

    async def send(self, payload: Mapping[str, Any]) -> int:
        cursor = await self._connection.execute(
            "select pgmq.send(%s, %s)",
            (self._name, Jsonb(payload)),
        )
        row = await cursor.fetchone()
        assert row is not None  # pgmq.send always returns the message id
        return int(row[0])

    async def read(self, *, visibility_timeout: int) -> QueueMessage | None:
        """Claim one message, hiding it from other readers for `visibility_timeout` seconds.

        Returns None when the queue has nothing visible — an empty queue is the
        normal case, not an error.
        """
        cursor = await self._connection.execute(
            "select msg_id, message, read_ct from pgmq.read(%s, %s::integer, 1)",
            (self._name, visibility_timeout),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return QueueMessage(id=int(row[0]), payload=row[1], read_count=int(row[2]))

    async def archive(self, message_id: int) -> bool:
        """Move the message to the archive table — the success terminal state.

        False means there was nothing to archive: the id is unknown, or someone
        already finished it.
        """
        cursor = await self._connection.execute(
            "select pgmq.archive(%s, %s::bigint)",
            (self._name, message_id),
        )
        row = await cursor.fetchone()
        return bool(row is not None and row[0])

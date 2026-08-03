"""The channel double for the pipeline level.

It records every send into a table and reads per-message directives from
another — the database is the IPC, and it crosses the process boundary for
free, which is exactly what the kill scenarios need.

The tables live in the `testing` schema, created by the pipeline conftest and
never by a production migration. The real adapters implement the same
`ChannelPort`; anything this fake can be told to do (fail, hang) is something
the real provider does uninvited.
"""

import psycopg

from agents_runtime.channels.port import ChannelPort, ClaimedSend

SCHEMA_SQL = """
create schema if not exists testing;
create table if not exists testing.fake_channel_sends (
    id              bigint generated always as identity primary key,
    outbox_id       uuid not null,
    idempotency_key text not null,
    to_phone_e164   text not null,
    payload         jsonb not null,
    sent_at         timestamptz not null default now()
);
create table if not exists testing.fake_channel_directives (
    idempotency_key text primary key,
    behavior        text not null
);
"""


class FakeChannel:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: psycopg.AsyncConnection | None = None

    async def _connection(self) -> psycopg.AsyncConnection:
        if self._conn is None or self._conn.closed:
            self._conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
        return self._conn

    async def send(self, send: ClaimedSend) -> str:
        conn = await self._connection()

        cursor = await conn.execute(
            "select behavior from testing.fake_channel_directives where idempotency_key = %s",
            (send.idempotency_key,),
        )
        row = await cursor.fetchone()
        behavior = row[0] if row else "deliver"

        if behavior == "fail_transient":
            raise ConnectionError("HTTP 503 fake channel told to fail")
        if behavior == "fail_permanent":
            raise ValueError("HTTP 400 fake channel told to reject")

        # The recording happens BEFORE the return, mirroring the real world:
        # the provider has the message the moment the API accepts it, whatever
        # happens to our process afterwards. Cenário 10 depends on this order.
        await conn.execute(
            """
            insert into testing.fake_channel_sends
                (outbox_id, idempotency_key, to_phone_e164, payload)
            values (%s, %s, %s, %s)
            """,
            (
                send.outbox_id,
                send.idempotency_key,
                send.to_phone_e164,
                psycopg.types.json.Jsonb(send.payload),
            ),
        )
        # hold_after_send: the provider HAS the message (recorded above) and
        # our process never learns it — held here until killed or released.
        # This is the exact ambiguity the unknown state exists for, made
        # deterministic (cenário 10).
        while behavior == "hold_after_send":
            cursor = await conn.execute(
                "select behavior from testing.fake_channel_directives"
                " where idempotency_key = %s",
                (send.idempotency_key,),
            )
            row = await cursor.fetchone()
            behavior = row[0] if row else "deliver"
            import asyncio

            await asyncio.sleep(0.05)

        return f"fake-wamid-{send.idempotency_key}"


def create_channel(dsn: str) -> ChannelPort:
    """The `AGENTS_CHANNEL` factory the subprocess harness points at."""
    return FakeChannel(dsn)

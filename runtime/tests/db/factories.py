"""Test data for the steel thread.

Every factory builds one row and returns what a test needs to talk about it.
They take a connection rather than opening one, so a test can build its graph
as the superuser and then attack it as `worker_role` — arranging with the
credential under test would make the arrangement part of what is being proved.

Uniqueness is per call, not per run. `channels_accounts` is UNIQUE on
`(type, phone_e164)` GLOBALLY — not per tenant — so two suites running against
the same database would collide on any fixed number. Everything generated here
derives from a fresh uuid.
"""

import uuid
from dataclasses import dataclass

import psycopg

# Brazil, so the generated number is shaped like a real one and passes the
# E.164 CHECK without the test having to know the regex.
_COUNTRY = "55"


def unique_phone() -> str:
    return f"+{_COUNTRY}{str(uuid.uuid4().int)[:9]}"


def unique_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class ConnectorAccount:
    id: uuid.UUID
    """The id the platform uses for this store — what resolves the tenant."""
    source_account_id: str


@dataclass(frozen=True)
class ChannelAccount:
    id: uuid.UUID
    phone_e164: str
    """The id the provider puts in its webhook — what resolves the tenant."""
    external_account_id: str


@dataclass(frozen=True)
class Thread:
    """A contact, a number and the conversation between them."""

    contact_id: uuid.UUID
    channel_account_id: uuid.UUID
    conversation_id: uuid.UUID


def create_connector_account(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    *,
    platform: str = "shopify",
) -> ConnectorAccount:
    source_account_id = unique_id("store")
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into public.connector_accounts (tenant_id, platform, source_account_id)
            values (%s, %s, %s)
            returning id
            """,
            (tenant_id, platform, source_account_id),
        )
        (account_id,) = cur.fetchone()

    return ConnectorAccount(id=account_id, source_account_id=source_account_id)


def create_channel_account(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    *,
    type: str = "cloud",
) -> ChannelAccount:
    phone = unique_phone()
    external_account_id = unique_id("wa")
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into public.channels_accounts
                (tenant_id, type, phone_e164, external_account_id, status)
            values (%s, %s, %s, %s, 'active')
            returning id
            """,
            (tenant_id, type, phone, external_account_id),
        )
        (account_id,) = cur.fetchone()

    return ChannelAccount(
        id=account_id, phone_e164=phone, external_account_id=external_account_id
    )


def create_contact(conn: psycopg.Connection, tenant_id: uuid.UUID) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "insert into public.contacts (tenant_id, phone_e164) values (%s, %s) returning id",
            (tenant_id, unique_phone()),
        )
        (contact_id,) = cur.fetchone()
    return contact_id


def create_thread(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    *,
    origin_occasion: str = "direct",
) -> Thread:
    contact_id = create_contact(conn, tenant_id)
    channel = create_channel_account(conn, tenant_id)

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into public.conversations
                (tenant_id, contact_id, channel_account_id, origin_occasion)
            values (%s, %s, %s, %s)
            returning id
            """,
            (tenant_id, contact_id, channel.id, origin_occasion),
        )
        (conversation_id,) = cur.fetchone()

    return Thread(
        contact_id=contact_id,
        channel_account_id=channel.id,
        conversation_id=conversation_id,
    )


def create_message(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    thread: Thread,
    *,
    direction: str = "inbound",
    seq: int = 1,
    text: str = "oi",
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into public.messages
                (tenant_id, conversation_id, direction, seq, channel, author_type, content)
            values (%s, %s, %s, %s, 'whatsapp_cloud', %s, %s)
            returning id
            """,
            (
                tenant_id,
                thread.conversation_id,
                direction,
                seq,
                "contact" if direction == "inbound" else "agent",
                psycopg.types.json.Jsonb({"text": text}),
            ),
        )
        (message_id,) = cur.fetchone()
    return message_id


def create_outbox_item(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    thread: Thread,
    *,
    status: str = "pending",
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into internal.message_outbox
                (tenant_id, conversation_id, contact_id, channel_account_id,
                 kind, payload, idempotency_key, status)
            values (%s, %s, %s, %s, 'reply', %s, %s, %s)
            returning id
            """,
            (
                tenant_id,
                thread.conversation_id,
                thread.contact_id,
                thread.channel_account_id,
                psycopg.types.json.Jsonb({"text": "resposta"}),
                unique_id("idem"),
                status,
            ),
        )
        (outbox_id,) = cur.fetchone()
    return outbox_id


def create_webhook_event(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    *,
    source: str = "shopify",
    source_account_id: str | None = None,
    external_event_id: str | None = None,
    event_type: str = "checkout_abandoned",
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into internal.webhook_events
                (source, source_account_id, external_event_id, tenant_id, event_type, payload)
            values (%s, %s, %s, %s, %s, %s)
            returning id
            """,
            (
                source,
                source_account_id or unique_id("store"),
                external_event_id or unique_id("evt"),
                tenant_id,
                event_type,
                psycopg.types.json.Jsonb({"raw": True}),
            ),
        )
        (event_id,) = cur.fetchone()
    return event_id

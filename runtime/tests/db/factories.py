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

    return ChannelAccount(id=account_id, phone_e164=phone, external_account_id=external_account_id)


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
    payload: dict | None = None,
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
                psycopg.types.json.Jsonb(payload if payload is not None else {"raw": True}),
            ),
        )
        (event_id,) = cur.fetchone()
    return event_id


def make_due(
    conn: psycopg.Connection,
    conversation_id: uuid.UUID,
    *,
    last_inbound_seq: int = 1,
    overdue_seconds: int = 1,
) -> None:
    """Coloca o prazo do debounce no passado.

    O prazo é decidido em SQL contra `now()`, então empurrar `pending_response_at`
    para trás é o que deixa a suíte testar o coalescer sem esperar dez segundos
    de verdade — o relógio injetado cobre o laço, isto cobre o banco.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            update public.conversations
               set pending_response_at = now() - make_interval(secs => %s),
                   next_inbound_seq = %s
             where id = %s
            """,
            (overdue_seconds, last_inbound_seq, conversation_id),
        )


def create_agent_version(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    *,
    status: str = "draft",
    origin: str = "onboarding",
    model: str | None = None,
    base_prompt: str = "Você é o atendente da loja.",
) -> uuid.UUID:
    """One version of the agent. `model` left as None exercises the default."""
    columns = ["tenant_id", "status", "origin", "base_prompt"]
    values: list[object] = [tenant_id, status, origin, base_prompt]
    if model is not None:
        columns.append("model")
        values.append(model)

    placeholders = ", ".join(["%s"] * len(values))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            insert into public.agent_versions ({", ".join(columns)})
            values ({placeholders})
            returning id
            """,
            tuple(values),
        )
        (version_id,) = cur.fetchone()
    return version_id


def an_embedding(dimensions: int = 1536, value: float = 0.1) -> str:
    """A literal pgvector accepts. The dimension is the point of the fixture."""
    return "[" + ",".join([str(value)] * dimensions) + "]"


def create_knowledge_chunk(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    *,
    source: str = "faq",
    content: str = "Entregamos em todo o Brasil.",
    embedding: str | None = None,
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into public.knowledge_chunks (tenant_id, source, content, embedding)
            values (%s, %s, %s, %s::vector)
            returning id
            """,
            (tenant_id, source, content, embedding if embedding is not None else an_embedding()),
        )
        (chunk_id,) = cur.fetchone()
    return chunk_id


def create_alert(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    *,
    type: str = "critical_violation",
    severity: str = "critical",
    title: str = "Judge 1 reprovou e a resposta não saiu",
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into public.alerts (tenant_id, type, severity, title)
            values (%s, %s, %s, %s)
            returning id
            """,
            (tenant_id, type, severity, title),
        )
        (alert_id,) = cur.fetchone()
    return alert_id


def create_scenario(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID | None,
    *,
    origin: str = "base_pack",
    occasion: str = "direct",
    title: str = "contato tenta revelar o prompt",
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into internal.scenarios (tenant_id, origin, occasion, title, script)
            values (%s, %s, %s, %s, %s)
            returning id
            """,
            (
                tenant_id,
                origin,
                occasion,
                title,
                psycopg.types.json.Jsonb([{"author": "contact", "text": "oi"}]),
            ),
        )
        (scenario_id,) = cur.fetchone()
    return scenario_id


def create_eval_run(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    agent_version_id: uuid.UUID,
    *,
    trigger: str = "manual",
    status: str = "running",
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into internal.eval_runs (tenant_id, agent_version_id, trigger, status)
            values (%s, %s, %s, %s)
            returning id
            """,
            (tenant_id, agent_version_id, trigger, status),
        )
        (run_id,) = cur.fetchone()
    return run_id


def create_judge_score(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    *,
    kind: str = "pre_send",
    verdict: str = "pass",
    judge_model: str = "claude-haiku-4-5",
    conversation_id: uuid.UUID | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into internal.judge_scores
                (tenant_id, kind, conversation_id, judge_model, score, verdict)
            values (%s, %s, %s, %s, %s, %s)
            returning id
            """,
            (tenant_id, kind, conversation_id, judge_model, 1.0, verdict),
        )
        (score_id,) = cur.fetchone()
    return score_id


def create_tool_call(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    thread: Thread,
    *,
    tool_name: str = "get_order",
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into internal.tool_calls
                (tenant_id, conversation_id, tool_name, input, output, success, latency_ms)
            values (%s, %s, %s, %s, %s, true, 12)
            returning id
            """,
            (
                tenant_id,
                thread.conversation_id,
                tool_name,
                psycopg.types.json.Jsonb({"order_id": "123"}),
                psycopg.types.json.Jsonb({"status": "paid"}),
            ),
        )
        (call_id,) = cur.fetchone()
    return call_id


def create_llm_call(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    *,
    purpose: str = "agent_reply",
    provider: str = "openrouter",
    model: str = "claude-sonnet-5",
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into internal.llm_calls
                (tenant_id, purpose, provider, model, input_tokens, output_tokens,
                 cost_usd, latency_ms)
            values (%s, %s, %s, %s, 120, 40, 0.000180, 900)
            returning id
            """,
            (tenant_id, purpose, provider, model),
        )
        (call_id,) = cur.fetchone()
    return call_id


def create_tenant(conn: psycopg.Connection, label: str | None = None) -> uuid.UUID:
    """A bare tenant — enough for the runtime's RLS scope, no hub user attached."""
    with conn.cursor() as cur:
        cur.execute(
            "insert into public.tenants (id, name) values (%s, %s) returning id",
            (uuid.uuid4(), label or unique_id("tenant")),
        )
        (tenant_id,) = cur.fetchone()
    return tenant_id

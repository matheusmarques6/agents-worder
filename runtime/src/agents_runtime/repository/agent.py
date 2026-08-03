"""O que o responder precisa ler antes de gerar (§2.1, §4.2, §4.3).

Cada função recebe uma conexão e não abre nem comita nenhuma: quem chama é dono
da transação curta e do `SET LOCAL app.tenant_id` dentro dela. No responder isso
importa mais do que em qualquer outro lugar — a FASE 2 roda **fora** de
transação, e uma leitura que abrisse a sua e a deixasse aberta seria uma
conexão presa atravessando a chamada do LLM (ADR-6).

Não há `where tenant_id = …` em lugar nenhum aqui: quem escopa é a policy
(CLAUDE.md, fronteiras de confiança). O que a conexão não pode ver, ela não vê.

A versão ativa vem do **índice parcial** do S2 (D6): não existe FK de cache em
`tenants`, então "qual versão está rodando" é pergunta que só o banco responde,
e dois mecanismos dizendo a mesma coisa divergiriam no pior dia.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg

from agents_runtime.agent_core.prompt import AgentConfig, ConversationView, TenantPolicy
from agents_runtime.agent_core.think_gate import PendingMessage

#: Quantas mensagens de histórico acompanham a pergunta. Conversa de meses não
#: cabe num prompt, e o que decide a próxima resposta é o fim dela.
DEFAULT_TRANSCRIPT_LIMIT = 20


@dataclass(frozen=True, slots=True)
class ActiveVersion:
    id: UUID
    config: AgentConfig

    @property
    def base_prompt(self) -> str:
        return self.config.base_prompt


@dataclass(frozen=True, slots=True)
class TenantSettings:
    """A política do tenant mais o que decide o modo shadow (S9b)."""

    policy: TenantPolicy
    shadow_until: datetime | None

    @property
    def primary_language(self) -> str:
        return self.policy.primary_language

    @property
    def never_say_ai(self) -> bool:
        return self.policy.never_say_ai


@dataclass(frozen=True, slots=True)
class ConversationState:
    view: ConversationView
    last_processed_seq: int

    @property
    def occasion(self) -> str:
        return self.view.occasion

    @property
    def contact_language(self) -> str | None:
        return self.view.contact_language


async def load_active_version(
    conn: psycopg.AsyncConnection, *, tenant_id: UUID
) -> ActiveVersion | None:
    """A versão em produção deste tenant, ou None — que significa "esta conta
    não pode responder", nunca "improvise um prompt"."""
    cursor = await conn.execute(
        """
        select id, model, base_prompt, scenario_prompts, enabled_tools
          from public.agent_versions
         where status = 'active'
        """
    )
    row = await cursor.fetchone()
    if row is None:
        return None

    return ActiveVersion(
        id=row[0],
        config=AgentConfig(
            model=row[1],
            base_prompt=row[2],
            scenario_prompts=dict(row[3] or {}),
            enabled_tools=tuple(row[4] or ()),
        ),
    )


async def load_tenant_policy(conn: psycopg.AsyncConnection, *, tenant_id: UUID) -> TenantSettings:
    cursor = await conn.execute(
        "select primary_language, never_say_ai, shadow_until from public.tenants where id = %s",
        (tenant_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise LookupError(f"tenant {tenant_id} is not visible to this connection")

    return TenantSettings(
        policy=TenantPolicy(primary_language=row[0], never_say_ai=row[1]),
        shadow_until=row[2],
    )


async def load_conversation_view(
    conn: psycopg.AsyncConnection, *, conversation_id: UUID
) -> ConversationState | None:
    cursor = await conn.execute(
        """
        select conversation.origin_occasion, contact.language, conversation.last_processed_seq
          from public.conversations conversation
          join public.contacts contact on contact.id = conversation.contact_id
         where conversation.id = %s
        """,
        (conversation_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None

    return ConversationState(
        view=ConversationView(occasion=row[0], contact_language=row[1]),
        last_processed_seq=row[2],
    )


async def load_pending_messages(
    conn: psycopg.AsyncConnection,
    *,
    conversation_id: UUID,
    after_seq: int,
    target_seq: int,
) -> tuple[PendingMessage, ...]:
    """A janela que o debounce coalesceu: `(after_seq, target_seq]`, inbound.

    Nem tudo o que existe na conversa (o que já foi respondido não se responde
    de novo) nem só a última (a rajada inteira é uma pergunta só). O que chegou
    DEPOIS do alvo é da próxima geração — respondê-lo agora seria exatamente a
    resposta constrangedora que o ADR-6 existe para impedir.
    """
    cursor = await conn.execute(
        """
        select author_type, content ->> 'text'
          from public.messages
         where conversation_id = %s
           and direction = 'inbound'
           and seq > %s
           and seq <= %s
         order by seq
        """,
        (conversation_id, after_seq, target_seq),
    )
    return tuple(
        PendingMessage(author=row[0], text=row[1] or "") for row in await cursor.fetchall()
    )


async def load_recent_transcript(
    conn: psycopg.AsyncConnection,
    *,
    conversation_id: UUID,
    limit: int = DEFAULT_TRANSCRIPT_LIMIT,
) -> tuple[PendingMessage, ...]:
    """As últimas `limit` mensagens, nas duas direções, na ordem em que
    aconteceram.

    Ordenar por `seq` embaralharia a conversa: inbound e outbound têm contadores
    SEPARADOS (`unique (conversation_id, direction, seq)`), então o relógio do
    banco é o único que sabe a ordem real.
    """
    cursor = await conn.execute(
        """
        select author_type, content ->> 'text'
          from (
            select author_type, content, created_at, direction
              from public.messages
             where conversation_id = %s
             -- Empate no relógio: a pergunta vem antes da resposta a ela.
             order by created_at desc, direction asc
             limit %s
          ) recent
         order by created_at asc, direction desc
        """,
        (conversation_id, limit),
    )
    return tuple(
        PendingMessage(author=row[0], text=row[1] or "") for row in await cursor.fetchall()
    )

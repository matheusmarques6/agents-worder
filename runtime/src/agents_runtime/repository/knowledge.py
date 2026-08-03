"""The merchant's knowledge — ingestion and similarity search (§2.5).

Like the rest of this layer, functions take a connection and never open or
commit one: the caller owns the transaction and the `SET LOCAL app.tenant_id`
that scopes it. In the responder that transaction is short and its own — a
retrieval must never sit inside a transaction that also spans the LLM call
(ADR-6).

**There is no `where tenant_id = …` in the search, and that is deliberate.**
Ownership is enforced by the policy, never by a hand-written clause (CLAUDE.md,
trust boundaries): a WHERE can be dropped in a refactor and nothing fails,
while dropping the policy fails the leak suite. The scope arrives through the
connection, and `tests/db/test_knowledge_retrieval.py` watches the policy doing
the work.

Cosine is the measure (`<=>`), matching the HNSW index built in S2 and the
embedding model D2 chose. Distance is turned into similarity at the edge so
call sites never have to remember which direction is better.

The vector is rendered as a literal and cast rather than adapted by a driver
extension: it is five lines against a new production dependency, and the burden
of proof is on whoever wants the dependency.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    id: UUID
    tenant_id: UUID
    source: str
    title: str | None
    content: str
    #: 1.0 is identical, 0.0 is unrelated — the inverse of the cosine distance
    #: the index sorts by.
    similarity: float


def _vector_literal(embedding: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(value)) for value in embedding) + "]"


async def ingest_chunk(
    conn: psycopg.AsyncConnection,
    *,
    tenant_id: UUID,
    source: str,
    content: str,
    embedding: Sequence[float],
    title: str | None = None,
    metadata: dict | None = None,
) -> UUID:
    """One chunk of knowledge, already split and already embedded.

    `metadata` is where the LGPD flag of the data dictionary lives (a chunk
    derived from a conversation enters the per-contact purge; a FAQ does not),
    so it is written from the first row instead of being backfilled later.
    """
    cursor = await conn.execute(
        """
        insert into public.knowledge_chunks
            (tenant_id, source, title, content, embedding, metadata)
        values (%s, %s, %s, %s, %s::vector, %s)
        returning id
        """,
        (
            tenant_id,
            source,
            title,
            content,
            _vector_literal(embedding),
            Jsonb(metadata if metadata is not None else {}),
        ),
    )
    return (await cursor.fetchone())[0]


async def search_knowledge(
    conn: psycopg.AsyncConnection,
    *,
    embedding: Sequence[float],
    limit: int = 5,
) -> tuple[KnowledgeChunk, ...]:
    """The nearest chunks first, bounded — the last layer of the prompt.

    Chunks without an embedding are excluded rather than ranked last: they have
    no measured distance at all, and putting them next to ones that do would be
    inventing an order.
    """
    cursor = await conn.execute(
        """
        select id, tenant_id, source, title, content,
               1 - (embedding <=> %(query)s::vector) as similarity
          from public.knowledge_chunks
         where embedding is not null
         order by embedding <=> %(query)s::vector
         limit %(limit)s
        """,
        {"query": _vector_literal(embedding), "limit": limit},
    )
    return tuple(
        KnowledgeChunk(
            id=row[0],
            tenant_id=row[1],
            source=row[2],
            title=row[3],
            content=row[4],
            similarity=float(row[5]),
        )
        for row in await cursor.fetchall()
    )

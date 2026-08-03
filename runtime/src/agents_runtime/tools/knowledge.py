"""`search_knowledge` — the merchant's own words, retrieved on demand.

The order of operations is the rule, not an implementation detail: embed first,
OUTSIDE any transaction, then open a short one to search. Embedding is a call
to a provider; a transaction held open across it is a database connection
hostage to somebody else's latency, which is exactly what ADR-6 forbids — and
the rule holds inside the responder, not only in the engine.

The query the model sends is the contact's question in the model's words, so it
is parsed strictly and nothing else in the arguments is read. Scope comes from
the context.
"""

from collections.abc import Mapping
from typing import Any

import psycopg

from agents_runtime.agent_core.llm import EMBEDDING_MODEL, EmbedderPort
from agents_runtime.repository import knowledge as knowledge_repo
from agents_runtime.repository.scope import scope_to_tenant
from agents_runtime.tools.base import ToolContext, ToolResult, require_text

#: How much knowledge may reach the prompt at once. Bounded because the last
#: layer of the prompt is not a place to paste a whole FAQ.
DEFAULT_LIMIT = 5


class SearchKnowledge:
    name = "search_knowledge"

    def __init__(self, embedder: EmbedderPort, *, limit: int = DEFAULT_LIMIT) -> None:
        self._embedder = embedder
        self._limit = limit

    async def __call__(
        self,
        conn: psycopg.AsyncConnection,
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> ToolResult:
        query = require_text(arguments, "query")

        # Outside any transaction, on purpose. See the module docstring.
        embedded = await self._embedder.embed([query], model=EMBEDDING_MODEL)

        async with conn.transaction():
            await scope_to_tenant(conn, context.tenant_id)
            chunks = await knowledge_repo.search_knowledge(
                conn, embedding=embedded.vectors[0], limit=self._limit
            )

        return ToolResult(
            tool=self.name,
            success=True,
            output={
                "chunks": [
                    {
                        "title": chunk.title,
                        "content": chunk.content,
                        "source": chunk.source,
                        "similarity": round(chunk.similarity, 4),
                    }
                    for chunk in chunks
                ]
            },
        )

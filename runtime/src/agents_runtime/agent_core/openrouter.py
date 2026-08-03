"""The one LLM adapter — OpenRouter, OpenAI-compatible (D1, decisão 79).

One door for chat and for embeddings: one key, one host in the network lock,
one place where a provider outage looks like a provider outage. The model is
whatever string the tenant's `agent_versions` row holds, so switching a tenant
from `claude-sonnet-5` to anything else is an UPDATE, never a deploy.

Errors surface as `RuntimeError("HTTP {status} …")` on purpose: the failure
classifier (unidade 4) reads the status code out of the message. Same shape the
Cloud API adapter uses — one vocabulary, one judge. Shapes the provider cannot
fill (a 2xx with no choice, an embedding short of a vector) are `ValueError`,
which that classifier already calls permanent: retrying them burns the ladder
on a call that will never produce an answer.

The credential is a PLATFORM secret — one key for every tenant — so it comes
from the environment rather than from Vault, which exists for per-tenant
secrets (the Meta token, E4). Absent, the process dies at startup instead of
discovering it on the first customer message.
"""

import os
from collections.abc import Sequence

import httpx

from agents_runtime.agent_core.llm import ChatRequest, ChatResult, EmbeddingResult, Usage

BASE_URL = "https://openrouter.ai/api/v1"

API_KEY_VARIABLE = "AGENTS_OPENROUTER_API_KEY"

# Generous: a reply with extended reasoning is slow by design, and the turn is
# protected by the lease keepalive, not by this number.
DEFAULT_TIMEOUT_SECONDS = 60.0


class OpenRouterLlm:
    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout),
            transport=transport,
        )

    async def chat(self, request: ChatRequest) -> ChatResult:
        body: dict = {
            "model": request.model,
            "messages": [
                {"role": message.role, "content": message.content} for message in request.messages
            ],
            # Cost only comes back when it is asked for, and the trail records
            # what was billed instead of guessing from a price table.
            "usage": {"include": True},
        }
        if request.think:
            # Only when the gate said so: paying for reasoning on every reply is
            # exactly the cost the think-gate exists to avoid.
            body["reasoning"] = {"enabled": True}

        data = await self._post("/chat/completions", body)

        choices = data.get("choices") or []
        if not choices:
            raise ValueError(f"openrouter: 2xx with no choice ({str(data)[:200]})")

        return ChatResult(
            text=choices[0]["message"]["content"],
            usage=_usage_of(data),
            model=data.get("model") or request.model,
        )

    async def embed(self, texts: Sequence[str], *, model: str) -> EmbeddingResult:
        data = await self._post("/embeddings", {"model": model, "input": list(texts)})

        entries = data.get("data") or []
        if len(entries) != len(texts):
            # Pairing a chunk with someone else's vector is a bug that only ever
            # shows up as bad retrieval, months later.
            raise ValueError(f"openrouter: asked for {len(texts)} embeddings, got {len(entries)}")

        ordered = sorted(entries, key=lambda entry: entry["index"])
        return EmbeddingResult(
            vectors=tuple(tuple(float(value) for value in entry["embedding"]) for entry in ordered),
            usage=_usage_of(data),
            model=data.get("model") or model,
        )

    async def _post(self, path: str, body: dict) -> dict:
        response = await self._client.post(path, json=body)

        if response.status_code >= 400:
            # The classifier reads the status out of this message; the body is
            # kept for forensics.
            raise RuntimeError(f"HTTP {response.status_code} {response.text[:300]}")

        return response.json()


def _usage_of(data: dict) -> Usage:
    usage = data.get("usage") or {}
    cost = usage.get("cost")
    return Usage(
        input_tokens=usage.get("prompt_tokens"),
        output_tokens=usage.get("completion_tokens"),
        cost_usd=float(cost) if cost is not None else None,
    )


def from_env() -> OpenRouterLlm:
    api_key = os.environ.get(API_KEY_VARIABLE)
    if not api_key:
        raise RuntimeError(
            f"{API_KEY_VARIABLE} is not set; there is no model to route to. "
            "The key is a platform secret, not a per-tenant one."
        )
    return OpenRouterLlm(api_key)

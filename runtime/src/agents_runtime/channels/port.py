"""The channel port — the one door out of the platform.

Everything the engine knows about WhatsApp fits in this file: a claimed send
goes in, a provider message id comes out. The real adapters (Cloud API,
Evolution) implement this in E1's final stretch; the pipeline suite implements
it with a fake that records into the database.

Nothing outside `channels` may call a provider API — that is the import-linter
contract, and this protocol is why nobody needs to.
"""

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ClaimedSend:
    """One row from `claim_outbox_batch` — everything a send needs, no second query."""

    outbox_id: UUID
    tenant_id: UUID
    channel_type: str
    channel_external_id: str
    to_phone_e164: str
    payload: dict[str, Any]
    idempotency_key: str
    attempt_count: int


class ChannelPort(Protocol):
    """Deliver one message; return the provider's id for it.

    Raise to signal failure — the sender classifies the exception (transient
    versus permanent, unidade 4) and decides between retry and giving up. The
    port itself never retries: retrying here would stack on top of the
    sender's backoff and multiply attempts invisibly.
    """

    async def send(self, send: ClaimedSend) -> str: ...

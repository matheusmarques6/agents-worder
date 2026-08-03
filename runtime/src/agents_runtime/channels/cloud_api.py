"""The real channel — WhatsApp Cloud API, rota A.

The shape of every call here was mined from worder1 (Bruno's previous
application), which ran this API in production: POST to
`{graph}/{version}/{phone_number_id}/messages` with a bearer token, wamid in
`messages[0].id`. What worder1 never used — and this adapter does — is
`biz_opaque_callback_data`: our idempotency_key travels in it so a status
webhook can resolve an `unknown` outbox row even when the sender died before
learning the wamid (ADR-8, decisão 59). Whether Meta echoes it faithfully is
exactly the pendência nº 2 of the architecture, owned by the `contract` suite.

Errors surface as `RuntimeError("HTTP {status} ...")` on purpose: the failure
classifier (unidade 4) reads the status code out of the message — 429/5xx
retry with backoff, 4xx give up. One classifier, one vocabulary.

Credentials: ONE access token from the environment, for the E1 demo against a
test number. Per-tenant tokens live in Vault behind `get_channel_secret()` and
arrive with T4/E4 — a decision recorded, not a gap forgotten.
"""

import os

import httpx

from agents_runtime.channels.port import ChannelPort, ClaimedSend

# v19.0 is the version worder1 ran in production — proven, not newest.
# Overridable per environment because Meta retires versions on a schedule.
DEFAULT_API_VERSION = "v19.0"

GRAPH_URL = "https://graph.facebook.com"


class CloudApiChannel:
    """One door out, per ADR: only senders hold an instance of this."""

    def __init__(
        self,
        access_token: str,
        *,
        api_version: str = DEFAULT_API_VERSION,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=f"{GRAPH_URL}/{api_version}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=httpx.Timeout(30.0),
            transport=transport,
        )

    async def send(self, send: ClaimedSend) -> str:
        response = await self._client.post(
            f"/{send.channel_external_id}/messages",
            json=self._payload_for(send),
        )

        if response.status_code >= 400:
            # The classifier reads the status out of this message; the body is
            # kept for the outbox's last_error forensics.
            raise RuntimeError(f"HTTP {response.status_code} {response.text[:300]}")

        # `or`, not a default: an empty list is present-but-useless, and the
        # 2xx-without-wamid test caught the IndexError the naive version hid.
        wamid = (response.json().get("messages") or [{}])[0].get("id")
        if not wamid:
            # A 200 without a message id is a contract violation by the
            # provider — permanent, never retried into a duplicate.
            raise ValueError(f"payload inválido: resposta 2xx sem wamid: {response.text[:200]}")
        return str(wamid)

    @staticmethod
    def _payload_for(send: ClaimedSend) -> dict:
        if "text" not in send.payload:
            # E1 sends text; templates and media arrive with the funnels (E3).
            # Guessing at an unknown shape would send SOMETHING to a customer.
            raise ValueError(f"payload inválido: sem 'text' — chaves {sorted(send.payload)}")

        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": send.to_phone_e164,
            "type": "text",
            "text": {"body": str(send.payload["text"])},
            # One key, two worlds (decisão 59): the status webhook echoes this
            # back, and that echo is what resolves an unknown without resending.
            "biz_opaque_callback_data": send.idempotency_key,
        }

    async def aclose(self) -> None:
        await self._client.aclose()


def from_env(dsn: str) -> ChannelPort:
    """The `AGENTS_CHANNEL` factory for production and the live demo.

    Missing credentials die at startup, loudly — the same doctrine as a broken
    factory spec: absent and broken are different states, and neither may
    quietly become "no sender in production".
    """
    token = os.environ.get("AGENTS_META_ACCESS_TOKEN")
    if not token:
        raise RuntimeError(
            "AGENTS_META_ACCESS_TOKEN não está definido — o canal Cloud API "
            "não tem como autenticar. Defina o token ou não configure AGENTS_CHANNEL."
        )
    return CloudApiChannel(
        token,
        api_version=os.environ.get("AGENTS_META_API_VERSION", DEFAULT_API_VERSION),
    )

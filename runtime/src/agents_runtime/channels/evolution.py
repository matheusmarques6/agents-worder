"""The second channel — Evolution API (rota B), the unofficial one.

Same mould as `cloud_api.py`, and the differences are all consequences of one
fact: **there is no provider here.** The Cloud API has a vendor, a versioned
contract and an address. Evolution is software somebody runs — us, or the
merchant — so the base URL is required configuration with no default, and this
file names no external host at all. That is why the network fitness function
(`tests/unit/test_no_provider_network.py`) still passes with only two adapters
in its allow-list: this adapter has nothing to allow.

What travels differently from the Cloud adapter, and why:

  * **`apikey` header, not `Authorization: Bearer`.** Evolution's own scheme.
  * **The instance name routes in the path** (`/message/sendText/{instance}`),
    so `channel_external_id` holds an instance name here and a `phone_number_id`
    there. One column, two meanings, and this file is the only place that knows.
  * **The `+` of E.164 does not travel.** Evolution addresses by JID and a JID
    has no `+`; sending one delivers to a number that does not exist, and the
    API answers 200 because to it the destination is just a string.
  * **There is no `biz_opaque_callback_data`.** Nothing carries our idempotency
    key into the provider and back, so the `unknown` correlation of ADR-8/decisão
    59 has NO evidence path on this channel. An `unknown` here stays unknown
    until a human looks — which is exactly what `review_stale_unknown` already
    does, and it is the honest state rather than a blind resend.

**RNF-032 — the secret.** The api key arrives by injection, never from source,
and production reads it from the environment through `from_env`. Per-account
keys live in Vault behind `channels_accounts.vault_secret_id` /
`get_channel_secret()` and arrive with T4/E4 — the same recorded decision the
Cloud adapter carries, not a gap forgotten. A single-key adapter is honest for a
single-instance pilot and dishonest the day two merchants share this process;
that day is T4's, and it is written down here so it cannot be discovered by
accident.

**What only the `contract` suite can settle** (R1 of the plan keeps the real
instance out of every blocking run):

  1. whether `key.id` is stable and is what the status webhook echoes;
  2. what a rate-limited or banned instance actually answers (the classifier
     assumes an HTTP status appears in the body — if Evolution answers 200 with
     an error inside, the classifier reads a success);
  3. whether a resend of the same text is deduplicated by the provider or
     delivered twice;
  4. whether reply buttons render at all on a current WhatsApp client — the
     question `_payload_for` refuses to guess at, below.
"""

import os

import httpx

from agents_runtime.channels.port import ChannelPort, ClaimedSend

#: The payload key `dispatch/copy.py` writes for the RF-033(a) consent pair.
#: Held as a literal for the same reason `cloud_api.py` holds it: an adapter
#: that imported the dispatch would be an adapter that knows how content is
#: composed. The two spellings are pinned by the consent test in
#: `tests/unit/test_evolution_channel.py`, which builds the payload through
#: `dispatch.consent` itself.
CONSENT_BUTTONS = "buttons"

#: Keys of our own bookkeeping that live in `message_outbox.payload` and are
#: nobody else's business. `generated` (D3c) exists so an audit can separate
#: model-written copy from approved template; forwarding it would leak our data
#: model into the body of somebody's message.
_TEXT = "text"


class EvolutionChannel:
    """One door out, per ADR-8: only senders hold an instance of this."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"apikey": api_key},
            timeout=httpx.Timeout(30.0),
            transport=transport,
        )

    async def send(self, send: ClaimedSend) -> str:
        payload = self._payload_for(send)

        response = await self._client.post(
            f"/message/sendText/{send.channel_external_id}", json=payload
        )

        if response.status_code >= 400:
            # The classifier reads the status out of this message; the body is
            # kept for the outbox's `last_error` forensics.
            raise RuntimeError(f"HTTP {response.status_code} {response.text[:300]}")

        message_id = (response.json().get("key") or {}).get("id")
        if not message_id:
            # A 2xx the provider cannot name is a contract violation —
            # permanent, never retried into a duplicate on somebody's phone.
            raise ValueError(
                f"payload inválido: resposta 2xx sem key.id: {response.text[:200]}"
            )
        return str(message_id)

    @staticmethod
    def _payload_for(send: ClaimedSend) -> dict:
        text = send.payload.get(_TEXT)
        if not isinstance(text, str) or not text.strip():
            raise ValueError(
                f"payload inválido: sem 'text' — chaves {sorted(send.payload)}"
            )

        if send.payload.get(CONSENT_BUTTONS):
            # RF-033(a) asks every touch to a non-consenting contact to carry
            # Autorizar/Bloquear. This channel can honour neither half: the
            # Baileys button shape is not reliable on a current WhatsApp client,
            # and the Evolution ingestion that would translate the tap back into
            # `button_reply.id` does not exist yet.
            #
            # Of the three ways out — guess at the button shape, send the copy
            # WITHOUT the choice, or refuse — only refusing suppresses a send
            # instead of creating one. Sending without the choice would be
            # selling to somebody who never consented and filing it as `sent`;
            # guessing contradicts the doctrine the Cloud adapter wrote down
            # ("never guess at a button"). So it fails, permanently and loudly,
            # into a row a human reads.
            raise ValueError(
                "payload inválido: toque com botões de consentimento não pode sair pela "
                "Evolution — o par Autorizar/Bloquear do RF-033(a) não tem forma confiável "
                "neste canal e não tem caminho de volta. Pendência nomeada do S7."
            )

        return {
            # No `+`: Evolution addresses by JID.
            "number": send.to_phone_e164.lstrip("+"),
            _TEXT: text,
        }

    async def aclose(self) -> None:
        await self._client.aclose()


def from_env(dsn: str) -> ChannelPort:
    """The `AGENTS_CHANNEL` factory for an Evolution-only process.

    Missing configuration dies at startup, loudly — the doctrine of the channel
    factory spec: absent and broken are different states, and neither may
    quietly become "no sender in production".
    """
    base_url = os.environ.get("AGENTS_EVOLUTION_BASE_URL")
    if not base_url:
        raise RuntimeError(
            "AGENTS_EVOLUTION_BASE_URL não está definido — a Evolution não tem "
            "endereço de fornecedor, o host é a instância que alguém subiu. "
            "Defina a URL ou não configure este canal."
        )

    api_key = os.environ.get("AGENTS_EVOLUTION_API_KEY")
    if not api_key:
        raise RuntimeError(
            "AGENTS_EVOLUTION_API_KEY não está definido — o canal Evolution não "
            "tem como autenticar. O segredo nunca mora no código (RNF-032)."
        )

    return EvolutionChannel(base_url, api_key)

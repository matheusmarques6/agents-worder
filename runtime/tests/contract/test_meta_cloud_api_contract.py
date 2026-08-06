"""The Meta Cloud API contract — pendência nº 2, open since E1.

**This suite sends a real WhatsApp message to a real number and costs real
money.** It is not on any gate, never runs in `pr.yml` or `main.yml`, and skips
itself unless three variables are set on purpose. Setting them IS the consent.

The question it answers is the one the whole `unknown` recovery path rests on:

> When we put our `idempotency_key` in `biz_opaque_callback_data`, does Meta
> echo it back on the status webhook?

Decisão 59 says that echo is the single piece of evidence that resolves an
`unknown` outbox row — a send whose HTTP response we never saw. If Meta does
not echo it, `sweep_outbox_unknown` can never correlate, every unknown degrades
to `manual_review`, and someone reads a queue by hand forever.

The reason it is still open: the worder1 (decisão 66) ran the Cloud API in
production for months and **never used this field** — it correlated by wamid.
So our design has no field evidence behind it, only the documentation. That is
exactly the kind of assumption a contract suite exists to break early, and
`core/plano-e2-agente-real.md` requires it answered **before the pilot (E7)**.

Written blocked on purpose, and never executed. Two consequences, stated so
nobody is surprised:

  1. it may fail on its first run for its own reasons — a bad assertion, a
     changed payload shape — and that first run is part of S12, not a formality;
  2. the ECHO half cannot be asserted from here at all. This process sends; the
     status webhook arrives at the Edge Function, in the database. What this
     file proves is that the send is accepted WITH the field. The echo is proved
     by reading `message_outbox` after the webhook lands — the demo of S12,
     written down in `docs/estado-da-execucao.md` rather than pretended here.

Variables, all three required:

    AGENTS_META_ACCESS_TOKEN     System User token (permanent — the 24h one is
                                 the trap of decisão 73)
    AGENTS_META_PHONE_NUMBER_ID  the sender id, NOT the phone number
    AGENTS_META_TEST_RECIPIENT   E.164 destination that consented to receive it
"""

import os
import uuid

import pytest

from agents_runtime.channels.cloud_api import CloudApiChannel
from agents_runtime.channels.port import ClaimedSend

ACCESS_TOKEN = os.environ.get("AGENTS_META_ACCESS_TOKEN")
PHONE_NUMBER_ID = os.environ.get("AGENTS_META_PHONE_NUMBER_ID")
TEST_RECIPIENT = os.environ.get("AGENTS_META_TEST_RECIPIENT")

pytestmark = pytest.mark.skipif(
    not (ACCESS_TOKEN and PHONE_NUMBER_ID and TEST_RECIPIENT),
    reason=(
        "needs AGENTS_META_ACCESS_TOKEN + AGENTS_META_PHONE_NUMBER_ID + "
        "AGENTS_META_TEST_RECIPIENT — this suite sends a real WhatsApp message"
    ),
)


def _send(idempotency_key: str) -> ClaimedSend:
    """The same shape `claim_outbox_batch` hands the sender, so the payload this
    test exercises is byte-for-byte the one production builds."""
    return ClaimedSend(
        outbox_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        channel_type="whatsapp_cloud",
        channel_external_id=str(PHONE_NUMBER_ID),
        to_phone_e164=str(TEST_RECIPIENT),
        payload={"text": "Teste de contrato do Agents Worder. Pode ignorar."},
        idempotency_key=idempotency_key,
        attempt_count=0,
    )


async def test_meta_accepts_a_send_carrying_our_idempotency_key() -> None:
    """The half this process can prove: the API accepts the payload with
    `biz_opaque_callback_data` set and returns a wamid.

    A rejection here would mean the field name or its position changed — and the
    correlation design would be broken before the echo question even matters.
    """
    channel = CloudApiChannel(str(ACCESS_TOKEN))
    key = f"contract-{uuid.uuid4()}"

    try:
        wamid = await channel.send(_send(key))
    finally:
        await channel.aclose()

    assert wamid.startswith("wamid."), (
        f"Meta returned {wamid!r} instead of a wamid — the sender stores this as "
        "provider_message_id and every status correlation reads it"
    )

    # The echo is NOT asserted here, and that is deliberate: it arrives at the
    # Edge Function minutes later, in another process. The `contract-` prefix is
    # what makes the key findable afterwards — grep the status webhook payload
    # (or `message_outbox.idempotency_key`) for it. That is the manual half of
    # the S12 proof, and printing it here would only trade a lint rule for a
    # line nobody reads.
    assert key.startswith("contract-")

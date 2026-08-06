"""Consent — the two buttons a touch carries, and the tap nobody interprets.

Both halves of the handshake live in this one module on purpose. The ids we
EMIT on a proactive touch and the ids we ACCEPT on the way back are the same two
strings, and a file that owned only one half would be a file free to drift from
its counterpart without anything failing: a button rendered as `consent_block`
and recognised as `block_consent` looks perfect in review and silently never
blocks anybody.

**Recognition is a lookup, never a judgement.** RF-033(a) says the contact taps
Bloquear and is removed. Which contact and which effect are authorisation
decisions, and `CLAUDE.md` is explicit that a message from a contact is hostile
input to the LLM — so the one place in this product where a stranger's message
changes a permission is the place least allowed to ask a model what the message
meant. What arrives is not prose: it is an id WE issued, echoed back by the
platform, matched against a table of two entries. Anything else is `None`, and
`None` means "an ordinary message" — the model reads it and answers it, and
nothing about consent moves.

A contact who somehow forged one of these ids would be blocking or authorising
**themselves**, which is precisely the decision the buttons exist to give them.
That is why the ids carry no signature: the blast radius of a forgery is the
forger's own consent.

The sibling shape of `ladder`, `think_gate` and `risk_gate`: deterministic
functions over data that was already loaded. Nothing here knows what a database
is, and nothing here calls a model.
"""

from collections.abc import Mapping
from typing import Any

#: What `recognize` returns. Words rather than a boolean because a third answer
#: (`None` — not a tap at all) already exists, and a boolean with a null is a
#: type that reads wrong at every call site.
AUTHORIZE = "authorize"
BLOCK = "block"

#: The ids that travel out on the buttons and come back on the reply. Prefixed
#: because they share a namespace with every other interactive id this product
#: will send (the second channel in S7, the screens in E5): an unprefixed
#: `block` is a collision waiting for a feature.
AUTHORIZE_BUTTON_ID = "consent_authorize"
BLOCK_BUTTON_ID = "consent_block"

#: PT-BR, and the words of RF-033 itself. WhatsApp caps a reply-button title at
#: 20 characters; both fit with room to spare.
AUTHORIZE_TITLE = "Autorizar"
BLOCK_TITLE = "Bloquear"

#: The payload key `dispatch/copy.py` writes and `channels/cloud_api.py` reads.
BUTTONS = "buttons"

#: `contacts.opt_status` — the only value that earns the buttons. `authorized`
#: already answered; `blocked` never receives a proactive at all (the ladder
#: stops it first), so asking again would be a question nobody hears.
PENDING = "pending"

#: `suppression_list.reason` / `created_by` for via (a). The block is the
#: platform acting on an explicit instruction, so `system` — `agent` is via (c),
#: where a model's reading of a sentence is what triggered it, and the trail has
#: to be able to tell those two apart.
EXPLICIT_BLOCK = "explicit_block"
CREATED_BY_SYSTEM = "system"

#: RF-033(b): three DISTINCT funnels ignored. Declared here and travelling as a
#: parameter into `internal.suppress_silent_contacts`, for the same reason the
#: ladder's windows travel — one copy of a canonical number is a rule, two are a
#: disagreement nobody notices.
SILENCE_FUNNEL_THRESHOLD = 3

_BY_ID = {
    AUTHORIZE_BUTTON_ID: AUTHORIZE,
    BLOCK_BUTTON_ID: BLOCK,
}


def buttons_for(opt_status: str | None) -> list[dict[str, str]] | None:
    """The Autorizar/Bloquear pair, or `None` when this touch does not ask.

    Order is the requirement's, and it is not cosmetic: a destructive choice
    offered first is a destructive choice taken by accident.
    """
    if opt_status != PENDING:
        return None

    return [
        {"id": AUTHORIZE_BUTTON_ID, "title": AUTHORIZE_TITLE},
        {"id": BLOCK_BUTTON_ID, "title": BLOCK_TITLE},
    ]


def recognize(content: Mapping[str, Any] | None) -> str | None:
    """`AUTHORIZE`, `BLOCK`, or `None` — from the stored message content.

    The shape is the one the ingestion writes into `public.messages.content`
    (`supabase/functions/ingest-meta/index.ts`): whatever else is in there, a
    tap leaves `button_reply.id`. Every guard below is the webhook doctrine one
    layer in — the row was built from a third party's payload, so a missing key,
    a wrong type, or an id we never issued are all simply "not a tap", never
    coerced into one.
    """
    if not isinstance(content, Mapping):
        return None

    reply = content.get("button_reply")
    if not isinstance(reply, Mapping):
        return None

    button_id = reply.get("id")
    if not isinstance(button_id, str):
        return None

    return _BY_ID.get(button_id)

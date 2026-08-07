"""What a due touch actually says — deterministically, and from the cadence.

The funnel's `touches` array is the source and the only source: `copy_base` is
text a human approved, `template_ref` names a template a human approved, and
this module picks the entry whose `n` matches the touch being fired. Nothing
here calls a model.

**That is a boundary, not an omission.** D3 gives anti-ban copy variation and
its deterministic validator to S7, and D10 puts them here rather than in the
sender, because `message_outbox.payload` is "the final content to send, with
anti-ban variation already applied" (dicionário §5.3). Until S7 arrives, the
final content IS the approved base — which is also why the dispatcher takes no
`TenantSlots` permit: the per-tenant semaphore of ADR-2 bounds concurrent LLM
work, and there is none. S7 adds the model call and the permit together, or it
adds a way for one tenant to burn the process's whole budget on funnels.

`generated` travels in the payload from today (D3c): the audit that separates
generated copy from approved template cannot be written retroactively, and a
column that starts existing halfway through has a blind period nobody remembers.
"""

from typing import Any

#: Keys of a cadence entry that are content rather than schedule. `n` and
#: `delay` are the schedule and never reach a contact.
_CTA = "cta"
_TEMPLATE = "template_ref"


class CadenceMissing(LookupError):
    """The funnel has no entry for this touch number.

    A configuration fault, not a protection: the cadence was edited after the
    touch was scheduled. It raises so the job climbs the retry ladder to the
    DLQ, where a human sees it — cancelling the touch instead would file a
    broken funnel under the vocabulary of contact protection, and the metric
    S11 asks for ("cancelled BY REASON") would quietly grow a bucket that means
    "our bug".
    """


def render(cadence: list[dict[str, Any]], touch_number: int) -> dict[str, Any]:
    """The outbox payload for touch `n` of this cadence.

    Deterministic by construction — same cadence, same number, same bytes — so
    that a redelivered job produces the identical payload under the identical
    idempotency key, and the outbox's UNIQUE stays the second lock on the door.
    """
    for entry in cadence:
        if _matches(entry, touch_number):
            return _payload(entry)

    raise CadenceMissing(f"the cadence has no touch n={touch_number}")


def _matches(entry: object, touch_number: int) -> bool:
    if not isinstance(entry, dict):
        return False
    try:
        return int(entry["n"]) == touch_number
    except (KeyError, TypeError, ValueError):
        # The schema's CHECK (S4 migration) makes this unreachable for a funnel
        # saved today. It is still here because the backend never trusts a
        # shape: an entry this code cannot read is not the touch we are firing.
        return False


def _payload(entry: dict[str, Any]) -> dict[str, Any]:
    text = entry.get("copy_base")
    if not isinstance(text, str) or not text.strip():
        raise CadenceMissing(f"touch n={entry.get('n')!r} has no copy_base to send")

    payload: dict[str, Any] = {"text": text, "generated": False}
    if isinstance(entry.get(_TEMPLATE), str):
        payload[_TEMPLATE] = entry[_TEMPLATE]
    if isinstance(entry.get(_CTA), str):
        payload[_CTA] = entry[_CTA]
    return payload

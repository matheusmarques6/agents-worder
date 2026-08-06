"""The `q_evals` consumer — outcomes are data, and pgmq is never left in limbo.

Same mould as the `q_domain_events` handler (decisão 74): the review returns a
DESFECHO, the handler archives on every one of them, and only an exception —
a bug, or the database being down — climbs to the retry ladder. A handler that
retried an outcome would re-judge a message that was already judged, on the
platform's money, forever.

Two things differ from the domain handler, and both are asserted here:

  * it TAKES A TENANT SLOT. The domain handler does not, because it is one
    short SQL call; this one calls a model, which is exactly what the cap of
    ADR-2 exists to bound;
  * a payload it cannot parse is archived rather than retried. A shape does not
    become valid on the second read, and there is no tenant in it to charge the
    wait to.

The handler is reached the way the process reaches it — through the factory
`app.evals_handler_for` — and not by re-implementing its body here. The S4/89
lesson: a test that rebuilds the wiring proves the rebuild.
"""

import uuid

import pytest

from agents_runtime.app import evals_handler_for
from agents_runtime.queueing import EVALS
from agents_runtime.queueing.engine_loop import Ack
from agents_runtime.queueing.tenant_slots import TenantSlots

pytestmark = pytest.mark.unit

TENANT = uuid.uuid4()

#: Every outcome the reviewer can legitimately report. The handler must not
#: know one from another — that is the whole content of "outcomes are data".
EVERY_OUTCOME = (
    "evaluated",
    "skipped_low_risk",
    "already_evaluated",
    "corrected",
    "correction_blocked",
    "no_channel",
)


class Message:
    """The shape `PgmqQueue.read` hands a handler."""

    def __init__(self, payload: dict, *, id: int = 1, read_count: int = 1) -> None:
        self.id = id
        self.payload = payload
        self.read_count = read_count


def a_payload(**overrides) -> dict:
    payload = {
        "tenant_id": str(TENANT),
        "conversation_id": str(uuid.uuid4()),
        "message_id": str(uuid.uuid4()),
    }
    payload.update(overrides)
    return payload


class Reviewer:
    """A review whose outcome — or explosion — is written in advance."""

    def __init__(self, outcome: object) -> None:
        self._outcome = outcome
        self.seen: list = []

    async def __call__(self, job):
        self.seen.append(job)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


class Outcome:
    def __init__(self, status: str) -> None:
        self.status = status


@pytest.mark.parametrize("status", EVERY_OUTCOME)
async def test_every_outcome_archives(status: str) -> None:
    handle = evals_handler_for(Reviewer(Outcome(status)), TenantSlots(3))

    ack = await handle(EVALS, Message(a_payload()))

    assert ack is Ack.ARCHIVE, (
        f"{status!r} left the message in the queue — an outcome the consumer "
        "retries is a message the platform pays to judge twice"
    )


async def test_a_payload_it_cannot_parse_is_archived_without_a_review() -> None:
    """A shape does not become valid on the second read."""
    review = Reviewer(Outcome("evaluated"))
    handle = evals_handler_for(review, TenantSlots(3))

    ack = await handle(EVALS, Message({"conversation_id": "not-a-uuid"}))

    assert ack is Ack.ARCHIVE
    assert review.seen == [], "a malformed job must not reach the reviewer"


async def test_a_broken_review_climbs_to_the_retry_ladder() -> None:
    """The other half: what IS a bug must not be archived into silence."""
    handle = evals_handler_for(Reviewer(RuntimeError("boom")), TenantSlots(3))

    with pytest.raises(RuntimeError):
        await handle(EVALS, Message(a_payload()))


class TestTheTenantSlot:
    async def test_a_full_tenant_postpones_the_job(self) -> None:
        """The cap of ADR-2 covers model calls, and this is one. Postponed —
        never dropped, never held in memory."""
        slots = TenantSlots(1)
        assert slots.try_acquire(TENANT)
        review = Reviewer(Outcome("evaluated"))

        ack = await evals_handler_for(review, slots)(EVALS, Message(a_payload()))

        assert ack is Ack.RETRY_SHORT
        assert review.seen == [], "the model was called by a tenant with no slot"

    async def test_another_tenant_keeps_flowing(self) -> None:
        slots = TenantSlots(1)
        assert slots.try_acquire(TENANT)

        ack = await evals_handler_for(Reviewer(Outcome("evaluated")), slots)(
            EVALS, Message(a_payload(tenant_id=str(uuid.uuid4())))
        )

        assert ack is Ack.ARCHIVE

    async def test_the_slot_is_released_after_the_review(self) -> None:
        slots = TenantSlots(1)
        handle = evals_handler_for(Reviewer(Outcome("evaluated")), slots)

        await handle(EVALS, Message(a_payload()))

        assert slots.try_acquire(TENANT), "the slot leaked: one job would cap the tenant forever"

    async def test_the_slot_is_released_even_when_the_review_explodes(self) -> None:
        slots = TenantSlots(1)
        handle = evals_handler_for(Reviewer(RuntimeError("boom")), slots)

        with pytest.raises(RuntimeError):
            await handle(EVALS, Message(a_payload()))

        assert slots.try_acquire(TENANT)

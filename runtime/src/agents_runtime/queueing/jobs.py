"""The job contracts the queues carry.

The payload shapes are fixed by the SQL that produces them (the coalescer for
`q_inbound`); this module is the Python mirror. Parsing is strict on purpose —
a job with a missing field is a contract violation, and a contract violation
classifies as permanent (unidade 4), which routes it to the DLQ instead of
retrying forever.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agents_runtime.obs import context


@dataclass(frozen=True, slots=True)
class InboundJob:
    """What the coalescer enqueued: respond to this conversation up to target_seq."""

    conversation_id: UUID
    generation: int
    target_seq: int
    tenant_id: UUID
    otel: dict[str, Any] | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "InboundJob":
        try:
            return cls(
                conversation_id=UUID(payload["conversation_id"]),
                generation=int(payload["generation"]),
                target_seq=int(payload["target_seq"]),
                tenant_id=UUID(payload["tenant_id"]),
                otel=payload.get("otel"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"malformed inbound job: {payload!r}") from error


@dataclass(frozen=True, slots=True)
class DomainEventJob:
    """What ingestion enqueued: apply this platform event's consequences.

    Only the id travels — tenant, type and payload live on the event row, and
    `apply_domain_event` reads them there. A fatter job would just be a copy
    that could drift from the truth.
    """

    webhook_event_id: int
    otel: dict[str, Any] | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "DomainEventJob":
        try:
            return cls(
                webhook_event_id=int(payload["webhook_event_id"]),
                otel=payload.get("otel"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"malformed domain event job: {payload!r}") from error


@dataclass(frozen=True, slots=True)
class ScheduledTouchJob:
    """What the dispatcher's minute tick enqueued: this touch is due.

    Ids only, and the tenant among them for the reason `InboundJob` carries it:
    without `SET LOCAL app.tenant_id` the worker cannot read its own touch, and
    the claim already knew whose it was. Every fact the ladder weighs is loaded
    when the job is picked up — a snapshot inside a payload would be as old as
    the queue wait, which is exactly the staleness the ladder exists to catch.
    """

    scheduled_touch_id: UUID
    tenant_id: UUID
    otel: dict[str, Any] | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ScheduledTouchJob":
        try:
            return cls(
                scheduled_touch_id=UUID(payload["scheduled_touch_id"]),
                tenant_id=UUID(payload["tenant_id"]),
                otel=payload.get("otel"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"malformed scheduled touch job: {payload!r}") from error

    def to_payload(self) -> dict[str, Any]:
        """The shape the claim task sends. Written here, next to the parser, so
        the producer and the consumer of this queue cannot disagree — the
        coalescer's payload is built in SQL and this one is not, which would
        otherwise leave the two halves in different files.

        `otel` appears only when there IS a context to carry (`obs.context`): the
        rule of this queue is that only ids travel, and a fixed `"otel": null`
        would be a key that asserts nothing riding on every job for ever."""
        return context.stamp(
            {
                "scheduled_touch_id": str(self.scheduled_touch_id),
                "tenant_id": str(self.tenant_id),
            },
            self.otel,
        )


@dataclass(frozen=True, slots=True)
class EvalJob:
    """What `conclude_turn` enqueued: audit the reply that was actually sent.

    Created in the same transaction as the outbox row (decisão 91), so the job
    exists exactly when the message exists — never for a draft the CAS refused.

    Ids only, the rule `apply_domain_event` already follows (decisão 74): the
    text, the score of the pre-send judge and everything else live on rows, and
    a copy inside a payload is a copy that can drift from what was said.
    """

    tenant_id: UUID
    conversation_id: UUID
    message_id: UUID
    otel: dict[str, Any] | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "EvalJob":
        try:
            return cls(
                tenant_id=UUID(payload["tenant_id"]),
                conversation_id=UUID(payload["conversation_id"]),
                message_id=UUID(payload["message_id"]),
                otel=payload.get("otel"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"malformed evaluation job: {payload!r}") from error

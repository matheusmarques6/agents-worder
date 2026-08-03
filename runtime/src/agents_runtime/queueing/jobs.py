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

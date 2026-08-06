"""The contract of `q_scheduled` — ids only, and both halves in one file.

`q_inbound`'s payload is built in SQL by the coalescer and parsed in Python, so
the two halves live in different languages and a test is the only place they
meet. This queue's producer is Python too (`dispatch_pass`), which is why
`to_payload` sits next to `from_payload` — and why the round trip is asserted
rather than assumed.

A missing field is a contract violation, which classifies as permanent and
routes to the DLQ instead of retrying forever (unidade 4).
"""

import uuid

import pytest

from agents_runtime.queueing.jobs import ScheduledTouchJob

pytestmark = pytest.mark.unit


class TestTheJobCarriesIdsAndTheTenant:
    def test_a_well_formed_payload_parses(self) -> None:
        touch_id, tenant_id = uuid.uuid4(), uuid.uuid4()

        job = ScheduledTouchJob.from_payload(
            {"scheduled_touch_id": str(touch_id), "tenant_id": str(tenant_id)}
        )

        assert job.scheduled_touch_id == touch_id
        assert job.tenant_id == tenant_id

    def test_the_tenant_travels_because_nothing_can_be_read_without_it(self) -> None:
        # `SET LOCAL app.tenant_id` comes before the first read (ADR-11), and
        # the claim already knew whose touch it was. Discovering it afterwards
        # would need a second cross-tenant door.
        with pytest.raises(ValueError):
            ScheduledTouchJob.from_payload({"scheduled_touch_id": str(uuid.uuid4())})

    def test_a_missing_touch_is_a_contract_violation(self) -> None:
        with pytest.raises(ValueError):
            ScheduledTouchJob.from_payload({"tenant_id": str(uuid.uuid4())})

    def test_a_field_that_is_not_a_uuid_is_refused(self) -> None:
        with pytest.raises(ValueError):
            ScheduledTouchJob.from_payload(
                {"scheduled_touch_id": "not-a-uuid", "tenant_id": str(uuid.uuid4())}
            )

    def test_what_the_sweep_sends_is_what_the_handler_reads(self) -> None:
        job = ScheduledTouchJob(scheduled_touch_id=uuid.uuid4(), tenant_id=uuid.uuid4())

        assert ScheduledTouchJob.from_payload(job.to_payload()) == job

    def test_no_fact_travels_in_the_payload(self) -> None:
        # Every fact the ladder weighs is loaded when the job is picked up. A
        # snapshot inside a payload would be exactly as old as the queue wait,
        # which is the staleness the ladder exists to catch.
        job = ScheduledTouchJob(scheduled_touch_id=uuid.uuid4(), tenant_id=uuid.uuid4())

        assert set(job.to_payload()) == {"scheduled_touch_id", "tenant_id"}

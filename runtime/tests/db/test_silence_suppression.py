"""RF-033 via (b) — o silêncio depois de três funis vira um FATO, não uma conta.

The requirement says the contact is REMOVED. That word is the whole design of
this file. "Has this contact ignored three funnels?" could be recomputed inside
the ladder every time a fourth touch came due, and it would even block the right
sends — but nobody could see it. The merchant's hub could not show it, the S11
metric ("cancelled by reason") could not count it, and the day somebody
refactors the ladder it would quietly stop being true. So the sweep writes a
row, exactly like the row an explicit block writes, and every reader of
suppression — the ladder, the CAS, the hub, the projection on `contacts` — gets
it for free.

Two definitions carry the suite:

  * **"distinct funnels", not "three touches".** A cadence of four touches in
    one funnel is one funnel being ignored, and RF-034 already says there is no
    per-funnel touch cap. Counting touches would remove a contact for ignoring a
    single conversation;
  * **"no response" is measured against the contact's last inbound message**,
    not per touch. Silence ACROSS the three, which is the literal reading of
    "silêncio após 3 disparos" — and it is also what makes a grace period
    unnecessary, and therefore uninvented: anybody who answers anything resets
    the count to zero, so the sweep may run one second after the third touch
    without punishing somebody who is typing.

The sweep runs as `worker_role` here, through `silence_pass`, because that is
the credential the composition gives it — a suite that swept as the superuser
would prove nothing about the grant.
"""

import uuid
from collections.abc import Iterator

import psycopg
import pytest

from agents_runtime.dispatch.consent import SILENCE_FUNNEL_THRESHOLD
from agents_runtime.queueing.suppression import silence_pass
from tests.db.factories import Thread, create_message, create_tenant, create_thread
from tests.db.factories_e3 import create_funnel, create_scheduled_touch, create_suppression
from tests.support.database import as_runtime_worker

#: The three occasions the schema allows, which is also exactly the number of
#: enabled funnels a tenant may have (`funnels_one_enabled_per_occasion`). The
#: threshold of RF-033 and the schema's ceiling meeting at 3 is a coincidence
#: worth naming rather than a rule.
OCCASIONS = ("cart_abandoned", "checkout_abandoned", "pix_pending")


@pytest.fixture
def tenant(admin: psycopg.Connection) -> Iterator[uuid.UUID]:
    tenant_id = create_tenant(admin)
    yield tenant_id
    with admin.cursor() as cur:
        cur.execute("delete from public.tenants where id = %s", (tenant_id,))


def _touched_by(
    admin: psycopg.Connection,
    tenant_id: uuid.UUID,
    thread: Thread,
    occasions: tuple[str, ...],
    *,
    sent_ago_seconds: int = 3600,
) -> None:
    """One sent touch per occasion — one funnel each, all already delivered."""
    for occasion in occasions:
        funnel = create_funnel(admin, tenant_id, occasion=occasion)
        create_scheduled_touch(
            admin,
            tenant_id,
            funnel.id,
            thread.contact_id,
            conversation_id=thread.conversation_id,
            status="sent",
            sent_ago_seconds=sent_ago_seconds,
        )


def _suppressions(admin: psycopg.Connection, contact_id: uuid.UUID) -> list[tuple]:
    with admin.cursor() as cur:
        cur.execute(
            "select reason, created_by from public.suppression_list where contact_id = %s",
            (contact_id,),
        )
        return cur.fetchall()


async def _sweep(dsn: str, **kwargs) -> int:
    async with as_runtime_worker(dsn) as conn:
        return await silence_pass(conn, **kwargs)


class TestThreeIgnoredFunnelsRemoveTheContact:
    async def test_the_silence_becomes_a_row(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        thread = create_thread(admin, tenant)
        _touched_by(admin, tenant, thread, OCCASIONS)

        removed = await _sweep(dsn)

        assert removed == 1
        assert _suppressions(admin, thread.contact_id) == [("no_response_after_3", "system")]

    async def test_the_removal_reaches_the_audit_trail(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """RNF-044 asks for supressões com motivo e timestamp. A suppression
        nobody caused is the one most worth being able to explain later: no
        human asked for it, so the trail is the only account of why it exists.
        """
        thread = create_thread(admin, tenant)
        _touched_by(admin, tenant, thread, OCCASIONS)

        await _sweep(dsn)

        with admin.cursor() as cur:
            cur.execute(
                """
                select action, actor_type, payload
                  from public.audit_log
                 where target_type = 'contact' and target_id = %s
                """,
                (thread.contact_id,),
            )
            (entry,) = cur.fetchall()
        action, actor_type, payload = entry
        assert action == "suppression.no_response_after_3"
        assert actor_type == "system"
        assert payload["distinct_funnels"] == SILENCE_FUNNEL_THRESHOLD

    async def test_the_projection_follows(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """The sweep writes only `suppression_list`; `contacts.opt_status` is
        derived. A via that forgot the projection would leave the merchant's
        screen saying `pending` about somebody the platform will never message
        again."""
        thread = create_thread(admin, tenant)
        _touched_by(admin, tenant, thread, OCCASIONS)

        await _sweep(dsn)

        with admin.cursor() as cur:
            cur.execute(
                "select opt_status from public.contacts where id = %s", (thread.contact_id,)
            )
            assert cur.fetchone() == ("blocked",)


class TestWhatDoesNotCountAsSilence:
    async def test_three_touches_of_the_SAME_funnel_are_one_funnel_ignored(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """RF-033 says três disparos em funis DISTINTOS. A four-touch cadence is
        one funnel talking, and RF-034 is explicit that there is no per-funnel
        touch cap — counting touches here would remove a contact for ignoring a
        single conversation."""
        thread = create_thread(admin, tenant)
        funnel = create_funnel(admin, tenant, occasion="cart_abandoned")
        for number in (1, 2, 3):
            create_scheduled_touch(
                admin,
                tenant,
                funnel.id,
                thread.contact_id,
                conversation_id=thread.conversation_id,
                touch_number=number,
                status="sent",
                sent_ago_seconds=3600,
            )

        assert await _sweep(dsn) == 0
        assert _suppressions(admin, thread.contact_id) == []

    async def test_two_funnels_are_not_three(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        thread = create_thread(admin, tenant)
        _touched_by(admin, tenant, thread, OCCASIONS[:2])

        assert await _sweep(dsn) == 0

    async def test_a_reply_after_the_touches_is_not_silence(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """The contact answered. Whatever they said, they are not silent, and a
        product that removed them for it would be removing the person it was
        trying to recover."""
        thread = create_thread(admin, tenant)
        _touched_by(admin, tenant, thread, OCCASIONS, sent_ago_seconds=3600)
        create_message(admin, tenant, thread, seq=1, text="oi, ainda estou pensando")

        assert await _sweep(dsn) == 0
        assert _suppressions(admin, thread.contact_id) == []

    async def test_a_reply_BEFORE_the_touches_does_not_save_them(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """The measure is the LAST inbound message, and the touches counted are
        the ones that came after it. A conversation from last month is not an
        answer to three funnels this month — reading it as one would make
        auto-suppression unreachable for exactly the contacts who once talked to
        us."""
        thread = create_thread(admin, tenant)
        create_message(admin, tenant, thread, seq=1, text="oi")
        with admin.cursor() as cur:
            cur.execute(
                "update public.messages set created_at = now() - interval '30 days'"
                " where conversation_id = %s",
                (thread.conversation_id,),
            )
        _touched_by(admin, tenant, thread, OCCASIONS, sent_ago_seconds=3600)

        assert await _sweep(dsn) == 1

    async def test_a_touch_that_never_went_out_does_not_count(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """Only `sent`. A touch the ladder cancelled is a touch the contact never
        saw, and silence towards a message nobody sent is not silence."""
        thread = create_thread(admin, tenant)
        for occasion in OCCASIONS:
            funnel = create_funnel(admin, tenant, occasion=occasion)
            create_scheduled_touch(
                admin,
                tenant,
                funnel.id,
                thread.contact_id,
                conversation_id=thread.conversation_id,
                status="cancelled",
                cancel_reason="rate_limit_24h",
            )

        assert await _sweep(dsn) == 0


class TestTheSweepItself:
    async def test_the_threshold_the_module_names_is_the_one_the_sweep_counts(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """The number of RF-033 lives in `dispatch/consent.py` and travels as a
        parameter — the discipline the ladder's windows established in S4. A
        literal in the SQL would be a second copy of a canonical number, free to
        drift in silence. Passing a different threshold has to change the
        answer, or the parameter is decoration."""
        thread = create_thread(admin, tenant)
        _touched_by(admin, tenant, thread, OCCASIONS[:2])

        assert await _sweep(dsn, threshold=SILENCE_FUNNEL_THRESHOLD) == 0
        assert await _sweep(dsn, threshold=2) == 1

    async def test_one_pass_sweeps_every_tenant(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """Cross-tenant by nature, in the mould of `claim_due_touches`: no
        filter parameter, because a caller able to ask for "only tenant X" would
        be an arbitrary cross-tenant query under another name (ADR-11). The
        proof is that a single pass — by ONE tenant's worker connection —
        removes contacts of both."""
        other = create_tenant(admin)
        try:
            mine = create_thread(admin, tenant)
            theirs = create_thread(admin, other)
            _touched_by(admin, tenant, mine, OCCASIONS)
            _touched_by(admin, other, theirs, OCCASIONS)

            assert await _sweep(dsn) == 2
            assert _suppressions(admin, mine.contact_id)
            assert _suppressions(admin, theirs.contact_id)
        finally:
            with admin.cursor() as cur:
                cur.execute("delete from public.tenants where id = %s", (other,))

    async def test_a_contact_already_suppressed_keeps_its_first_reason(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """Suppression is a state, not a log: one row per contact. The via that
        got there first is the true story, and the sweep does not report a
        removal it did not perform — a count that included them would inflate
        the S11 metric with the same contact every fifteen minutes, forever."""
        thread = create_thread(admin, tenant)
        _touched_by(admin, tenant, thread, OCCASIONS)
        create_suppression(admin, tenant, thread.contact_id, reason="intent_optout")

        assert await _sweep(dsn) == 0
        assert _suppressions(admin, thread.contact_id) == [("intent_optout", "system")]

    async def test_a_second_pass_finds_nothing_new(
        self, dsn: str, admin: psycopg.Connection, tenant: uuid.UUID
    ) -> None:
        """The sweep runs every fifteen minutes forever. Idempotence is not a
        nicety here: without it the trail would gain one entry per pass for a
        fact that happened once."""
        thread = create_thread(admin, tenant)
        _touched_by(admin, tenant, thread, OCCASIONS)

        assert await _sweep(dsn) == 1
        assert await _sweep(dsn) == 0

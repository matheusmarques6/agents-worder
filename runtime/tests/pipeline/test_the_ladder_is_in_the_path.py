"""A escada está no caminho — e este arquivo é o que reprova se ela sair.

The sabotage this exists for is the one worth doing: take `ladder.decide` out of
`run_touch` and write straight to the outbox. Every other test of this step
survives that, and the reason is worth writing down rather than discovering
later — the compare-and-set revalidates every rung the ladder decides, and the
conflict path runs the ladder again to name the reason, so for suppression, a
payment, staleness, the 24h count, the 72h cooldown and the tier, the two gates
say the same word and removing one is invisible in the outcome.

There is exactly one rung the CAS cannot re-assert, and D9 says so out loud:
**quota**. There is no `quota_rules` table to write a conjunct against — a
placeholder table nobody tests is worse than no table — so `Guards` deliberately
omits it. Which makes the quota rung the place where the ladder is load-bearing
and nothing else is:

    ladder in the path   → the touch is cancelled with `quota_exceeded`
    ladder out of the path → the touch goes out

`quota_exceeded` is a word the SQL cannot produce. That is the whole assertion.

Note what is NOT patched: the ladder is the real one, `run_touch` is the real
one, the SQL is the real one. Only `quota.has_headroom` is spied — the same
device `tests/unit/test_quota_enforcement_point.py` uses, and the only way to
exercise an enforcement point whose data source does not exist yet. The day
plans exist, the spy is replaced by a row and this test keeps its meaning.
"""

import psycopg
import pytest

from agents_runtime import quota
from agents_runtime.clock import SystemClock
from agents_runtime.queueing.dispatcher import run_touch
from agents_runtime.queueing.engine_loop import Ack
from agents_runtime.queueing.jobs import ScheduledTouchJob
from tests.db.factories import create_tenant, create_thread
from tests.db.factories_e3 import create_funnel, create_scheduled_touch
from tests.support.database import as_runtime_worker

CADENCE = [{"n": 1, "delay": "PT0S", "copy_base": "Vi que ficou algo no carrinho."}]


@pytest.fixture
def job(sync_admin: psycopg.Connection) -> ScheduledTouchJob:
    tenant_id = create_tenant(sync_admin)
    thread = create_thread(sync_admin, tenant_id)
    funnel = create_funnel(sync_admin, tenant_id, touches=CADENCE)
    touch_id = create_scheduled_touch(
        sync_admin,
        tenant_id,
        funnel.id,
        thread.contact_id,
        conversation_id=thread.conversation_id,
        due_in_seconds=-60,
        status="enqueued",
    )
    return ScheduledTouchJob(scheduled_touch_id=touch_id, tenant_id=tenant_id)


def _outcome(conn: psycopg.Connection, job: ScheduledTouchJob) -> tuple:
    return conn.execute(
        """
        select t.status, t.cancel_reason, (select count(*) from internal.message_outbox)
          from public.scheduled_touches t where t.id = %s
        """,
        (job.scheduled_touch_id,),
    ).fetchone()


async def test_a_touch_the_plan_does_not_allow_never_reaches_the_outbox(
    dsn: str,
    sync_admin: psycopg.Connection,
    job: ScheduledTouchJob,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The one rung with no conjunct behind it. If the decision is skipped, the
    # CAS finds every fact in order and the touch goes out.
    monkeypatch.setattr(quota, "has_headroom", lambda _allowance: False)

    async with as_runtime_worker(dsn) as conn:
        ack = await run_touch(conn, job, clock=SystemClock())

    assert ack is Ack.ARCHIVE
    assert _outcome(sync_admin, job) == ("cancelled", "quota_exceeded", 0)


async def test_the_same_touch_goes_out_when_the_plan_allows_it(
    dsn: str, sync_admin: psycopg.Connection, job: ScheduledTouchJob
) -> None:
    """The control: the default answer is unlimited (RF-073), so nothing above
    is an artefact of the arrangement."""
    async with as_runtime_worker(dsn) as conn:
        ack = await run_touch(conn, job, clock=SystemClock())

    assert ack is Ack.ARCHIVE
    assert _outcome(sync_admin, job) == ("sent", None, 1)


async def test_the_denial_is_written_before_any_send_is_attempted(
    dsn: str,
    sync_admin: psycopg.Connection,
    job: ScheduledTouchJob,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A denied touch does not take the write path at all.

    `dispatch_touch` marks nothing on a refusal, but it does claim the row with
    `FOR UPDATE` — so "the touch is still `enqueued`" would not distinguish the
    two paths. What does distinguish them is the reason: only the ladder can say
    `quota_exceeded`, and it can only be said before a write that would have
    succeeded."""
    monkeypatch.setattr(quota, "has_headroom", lambda _allowance: False)

    async with as_runtime_worker(dsn) as conn:
        await run_touch(conn, job, clock=SystemClock())

    messages = sync_admin.execute(
        "select count(*) from public.messages where direction = 'outbound'"
    ).fetchone()[0]

    assert messages == 0

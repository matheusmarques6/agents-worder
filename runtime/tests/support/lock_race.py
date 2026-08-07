"""Staging a race for real: hold the row, inject the fact, let go.

A test that calls the ladder twice with different facts proves that the ladder
is a function. It proves nothing about D2, whose whole claim is about an
INSTANT — the one between a decision taken outside every transaction and a write
that has to refuse when the world moved underneath it.

So this helper does not simulate the race, it stages it. The recipe:

  1. a spectator connection takes `SELECT ... FOR UPDATE` on the scheduled touch
     and holds its transaction open;
  2. the code under test runs for real — it loads the snapshot (a plain read,
     which the lock does not block), the ladder decides, and then the write
     reaches `internal.dispatch_touch`, whose first statement is `SELECT ... FOR
     UPDATE` on the same row. It BLOCKS. The decision has happened; the write
     has not;
  3. `wait_until_blocked` is how the test knows it is there — `pg_stat_activity`
     says a backend is waiting on a lock, which is a fact about the server, not
     a sleep and a hope;
  4. the test injects the fact — a message, a payment, a block — and commits;
  5. the lock is released, `dispatch_touch` proceeds, and its CAS runs as a NEW
     statement, which under READ COMMITTED takes a NEW snapshot and therefore
     sees everything that was committed while it waited.

Step 5 is the reason `dispatch_touch` is two statements rather than one clever
UPDATE, and this helper is what makes the difference observable: a single
UPDATE's EvalPlanQual re-check would re-evaluate its qual against the updated
row but keep the original snapshot for its subqueries, so the message that
landed on ANOTHER table would be invisible and the touch would go out.
"""

import asyncio
import time

import psycopg

BLOCKED_BACKENDS = """
select count(*)
  from pg_stat_activity
 where wait_event_type = 'Lock'
   and state = 'active'
   and query ilike %s
"""


async def wait_until_blocked(
    conn: psycopg.AsyncConnection, *, needle: str, give_up_after: float = 15.0
) -> None:
    """Block until the server says somebody is waiting on a lock for `needle`.

    The observable is the database's own view of itself — the same discipline
    the pipeline harness follows for everything else: never a fixed sleep, never
    log parsing, always a predicate anybody can poll.
    """
    deadline = time.monotonic() + give_up_after
    while time.monotonic() < deadline:
        cursor = await conn.execute(BLOCKED_BACKENDS, (f"%{needle}%",))
        if (await cursor.fetchone())[0] > 0:
            return
        await asyncio.sleep(0.02)
    raise TimeoutError(f"no backend ever blocked on a lock while running {needle!r}")

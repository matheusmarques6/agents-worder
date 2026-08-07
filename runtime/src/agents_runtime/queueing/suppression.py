"""Via (b) of RF-033 — silence after three funnels becomes a recorded fact.

The sweep, not the dispatcher, and that is the point of the step. "This contact
ignored three funnels" could be computed inside the ladder every time a fourth
touch came due, and it would even block the right sends — but RF-033 says the
contact is REMOVED, and a negative recomputed at dispatch time is not a
removal. Nobody can see it, the merchant's hub cannot show it, the S11 metric
cannot count it, and the day the ladder is refactored it silently stops being
true. So it is a row, written by a pass of its own, exactly like the row an
explicit block writes.

Cross-tenant by nature, so `internal.suppress_silent_contacts` is SECURITY
DEFINER with no filter parameter — the mould of `claim_due_touches` and the
reading of ADR-11 the milestone already settled: a caller that could ask for
"only tenant X" would be an arbitrary cross-tenant query under another name.

"No response" means no inbound message from the contact since the EARLIEST of
the counted touches — silence across the three, not silence per touch. That
choice is what makes a grace period unnecessary and therefore uninvented: a
contact who answered anything at all resets the count to zero, so the sweep can
run one second after the third touch without punishing somebody who is typing.
"""

import psycopg

from agents_runtime.dispatch.consent import SILENCE_FUNNEL_THRESHOLD
from agents_runtime.repository import consent as consent_repo


async def silence_pass(
    conn: psycopg.AsyncConnection, *, threshold: int = SILENCE_FUNNEL_THRESHOLD
) -> int:
    """One sweep. Returns how many contacts were removed."""
    async with conn.transaction():
        return await consent_repo.suppress_silent_contacts(conn, distinct_funnels=threshold)

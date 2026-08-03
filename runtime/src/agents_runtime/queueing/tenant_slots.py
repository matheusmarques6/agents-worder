"""The per-tenant concurrency cap — three turns at once, per merchant.

One noisy tenant must never occupy every worker while the others wait: the
cap is what keeps a burst on one store from becoming latency on all of them.
A job that finds its tenant full is postponed (`set_vt` short), never
dropped, never queued in memory.

VALID ONLY WHILE THE RUNTIME IS A SINGLE ASYNCIO PROCESS (ADR-2). The counts
live in this process's memory; a second process would have its own counts
and the real concurrency would be the sum. Going multi-process REQUIRES
migrating this to a distributed lease (Postgres `tenant_slots` or Redis)
first — that is the CLAUDE.md warning, travelling in the code it applies to.
"""

from uuid import UUID


class TenantSlots:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active: dict[UUID, int] = {}

    def try_acquire(self, tenant_id: UUID) -> bool:
        """True and counted, or False and untouched. Never blocks: the caller's
        move on a full tenant is to postpone the job, not to hold a worker."""
        current = self._active.get(tenant_id, 0)
        if current >= self._limit:
            return False
        self._active[tenant_id] = current + 1
        return True

    def release(self, tenant_id: UUID) -> None:
        remaining = self._active.get(tenant_id, 0) - 1
        if remaining <= 0:
            self._active.pop(tenant_id, None)
        else:
            self._active[tenant_id] = remaining

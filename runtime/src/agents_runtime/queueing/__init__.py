"""pgmq access, backoff, heartbeat, weighted polling and per-tenant semaphores.

Every message read ends in archive (success), set_vt with exponential backoff
plus jitter (transient) or the queue DLQ (permanent). pgmq is never left in
limbo.
"""

# The queue names are part of the schema (supabase/migrations), so the runtime
# never spells one out at a call site: a typo there reads as "empty queue"
# forever.
#
# The weights of arquitetura §2 hang off this order — 8 : 4 : 2 : 1 — and the
# weighted polling that uses them arrives with its own tests.
INBOUND = "q_inbound"
DOMAIN_EVENTS = "q_domain_events"
SCHEDULED = "q_scheduled"
EVALS = "q_evals"

WORK_QUEUES = (INBOUND, DOMAIN_EVENTS, SCHEDULED, EVALS)

# Every read ends in archive, set_vt with backoff, or here. One dead letter
# queue per work queue, named after it — a shared DLQ would mix jobs whose
# retry limits and owners differ.
DEAD_LETTER = tuple(f"{queue}_dlq" for queue in WORK_QUEUES)

ALL_QUEUES = WORK_QUEUES + DEAD_LETTER

"""pgmq access, backoff, heartbeat, weighted polling and per-tenant semaphores.

Every message read ends in archive (success), set_vt with exponential backoff
plus jitter (transient) or the queue DLQ (permanent). pgmq is never left in
limbo.
"""

# The queue names are part of the schema (supabase/migrations), so the runtime
# never spells one out at a call site: a typo there reads as "empty queue"
# forever. Only `q_inbound` exists so far — the other three of arquitetura §2
# arrive with the weighted polling in E1.
INBOUND = "q_inbound"

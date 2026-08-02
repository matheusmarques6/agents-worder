"""pgmq access, backoff, heartbeat, weighted polling and per-tenant semaphores.

Every message read ends in archive (success), set_vt with exponential backoff
plus jitter (transient) or the queue DLQ (permanent). pgmq is never left in
limbo.
"""

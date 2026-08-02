"""Funnels, cadence, follow-up, suppression and rate limits.

Before EVERY proactive send, in this order: suppression -> quota -> staleness
-> rate limits. Reactive replies are never rate-limited (anti-flood is the
debounce only).
"""

# SABOTAGE N2 (plano §E0-12) — SQL fora da camada de repositório.
NEXT_TOUCH = "SELECT id FROM conversations WHERE tenant_id = %s AND pending_response_at < now()"

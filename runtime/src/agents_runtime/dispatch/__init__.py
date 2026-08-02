"""Funnels, cadence, follow-up, suppression and rate limits.

Before EVERY proactive send, in this order: suppression -> quota -> staleness
-> rate limits. Reactive replies are never rate-limited (anti-flood is the
debounce only).
"""

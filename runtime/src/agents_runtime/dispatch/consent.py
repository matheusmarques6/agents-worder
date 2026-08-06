"""Consent — the vocabulary of RF-033, pure and without I/O.

The sibling shape of `ladder`, `think_gate` and `risk_gate`: constants and
deterministic functions over data that was already loaded. Nothing here knows
what a database is.
"""

#: RF-033(b): three DISTINCT funnels ignored. Declared here and travelling as a
#: parameter into `internal.suppress_silent_contacts`, for the same reason the
#: ladder's windows travel — one copy of a canonical number is a rule, two are a
#: disagreement nobody notices.
SILENCE_FUNNEL_THRESHOLD = 3

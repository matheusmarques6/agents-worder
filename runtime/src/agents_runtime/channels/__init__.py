"""Outbox senders: typing, buttons, humanised delay, anti-ban.

The ONLY module allowed to call a messaging provider API (Meta Cloud API,
Evolution). `agent_core` and `dispatch` never send — they write to
`message_outbox` inside the PHASE 3 transaction and this module delivers.
"""

# SABOTAGE N1 (plano §E0-12) — este import viola a independência entre
# channels e connectors. O job `boundaries` tem de reprovar por isto.
import agents_runtime.connectors  # noqa: F401

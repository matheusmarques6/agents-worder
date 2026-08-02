"""Outbox senders: typing, buttons, humanised delay, anti-ban.

The ONLY module allowed to call a messaging provider API (Meta Cloud API,
Evolution). `agent_core` and `dispatch` never send — they write to
`message_outbox` inside the PHASE 3 transaction and this module delivers.
"""

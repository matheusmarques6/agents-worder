"""Layered prompt, slots, think-gate and the LLM call.

Owns the three-phase cycle of ADR-6: claim (short transaction) -> work (OUTSIDE
any transaction) -> conclusion (short transaction with the extended CAS). A
message arriving during the LLM call invalidates the draft; the draft is
discarded, never sent.
"""

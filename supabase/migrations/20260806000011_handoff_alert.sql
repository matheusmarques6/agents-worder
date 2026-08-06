-- E3 · S9 — `escalate_to_human` needs a place to leave the customer.
--
-- The conversation state for a handover already exists (`conversations.state =
-- 'humano'`, migration 0003) and nothing until now was able to enter it. Moving
-- the state alone is not enough, though: a conversation waiting for a person is
-- invisible until the E5 inbox is built, and "invisible until a later
-- milestone" is how a customer waits forever. So the handover also opens a row
-- in `alerts`, which is the surface merchants already have.
--
-- Expand-contract: widening a CHECK accepts strictly more than before, so the
-- N-1 runtime keeps working against this schema — every value it can write is
-- still accepted. Nothing is dropped and nothing is rewritten.

alter table public.alerts drop constraint alerts_type_check;

alter table public.alerts add constraint alerts_type_check
    check (type in ('critical_violation', 'queue_depth', 'queue_age', 'dlq',
                    'outbox_unknown', 'outbox_review', 'meta_tier',
                    'connector_error', 'lease_expired',
                    -- New, and the only addition: the agent handed a
                    -- conversation to a person.
                    'handoff'));

comment on constraint alerts_type_check on public.alerts is
    'E3 S9 added `handoff`: the agent stepped aside and a human is expected.';

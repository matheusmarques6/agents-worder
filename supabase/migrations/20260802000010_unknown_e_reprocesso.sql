-- E1 · PR 4 — the unknown transitions and the way back from the DLQ.
--
-- ADR-8's honest promise, materialised: no send is ever lost, duplication is
-- minimised, and when the machine cannot KNOW whether a message left, it asks
-- a human instead of guessing. Between duplicating a customer's message and
-- requesting review, the engine chooses review — always.

-- ---------------------------------------------------------------------------
-- sending → unknown: the sweep
-- ---------------------------------------------------------------------------
-- A `sending` row whose claim lease expired belongs to a sender that died (or
-- stalled) mid-request. We cannot know whether the provider got the message —
-- the fake's write-before-return order mirrors exactly this ambiguity. The
-- transition is STATE ONLY: nothing here resends, nothing here ever will.
--
-- `locked_by` is kept as forensics (whose claim died); `locked_until` is
-- cleared so the row can never look claim-expired twice. A LATE sender that
-- finally gets its answer cannot mark_sent from here — those functions demand
-- status = 'sending' — so correlation is the only road out. Conservative on
-- purpose: one road, one authority.
create function internal.sweep_outbox_unknown()
    returns integer
    language sql
    security definer
    set search_path = pg_catalog, internal
as $$
    with swept as (
        update internal.message_outbox
           set status = 'unknown',
               locked_until = null,
               last_error = 'send lease expired mid-request; outcome unknown'
         where status = 'sending'
           and locked_until < now()
        returning 1
    )
    select count(*)::integer from swept
$$;

-- ---------------------------------------------------------------------------
-- unknown | sending → sent | failed: correlation
-- ---------------------------------------------------------------------------
-- DECISION RECORDED: `biz_opaque_callback_data` IS the idempotency_key — one
-- key, two worlds. The status webhook echoes it back, and that echo is the
-- only evidence that resolves an unknown.
--
-- `sending` is accepted too: a status webhook can outrun the sender's own
-- mark_sent, and evidence does not get less true for arriving early.
create function internal.correlate_outbox_status(
    p_idempotency_key     text,
    p_status              text,
    p_provider_message_id text default null
)
    returns boolean
    language plpgsql
    security definer
    set search_path = pg_catalog, internal
as $$
begin
    if p_status not in ('sent', 'failed') then
        raise exception 'unknown correlation status: %', p_status;
    end if;

    update internal.message_outbox
       set status = p_status,
           provider_message_id = coalesce(p_provider_message_id, provider_message_id),
           sent_at = case when p_status = 'sent' then now() else sent_at end,
           locked_by = null,
           locked_until = null,
           last_error = case when p_status = 'sent' then null else last_error end
     where idempotency_key = p_idempotency_key
       and status in ('sending', 'unknown');

    return found;
end
$$;

-- ---------------------------------------------------------------------------
-- unknown → manual_review: the window closes
-- ---------------------------------------------------------------------------
-- No evidence arrived. A human decides — the alert that carries this to a
-- screen is T4/E6, and its absence today is a recorded pendência, not a gap
-- nobody noticed. `request_started_at` anchors the window: it marks the last
-- moment we KNOW the send was attempted.
create function internal.review_stale_unknown(
    p_review_after interval default interval '5 minutes'
)
    returns integer
    language sql
    security definer
    set search_path = pg_catalog, internal
as $$
    with flagged as (
        update internal.message_outbox
           set status = 'manual_review'
         where status = 'unknown'
           and request_started_at < now() - p_review_after
        returning 1
    )
    select count(*)::integer from flagged
$$;

-- ---------------------------------------------------------------------------
-- DLQ → origin queue: the way back
-- ---------------------------------------------------------------------------
-- The DLQ is a waiting room, not a cemetery. Reprocessing pops each entry and
-- re-sends the ORIGINAL payload — the forensic fields the loop added
-- (error_class, last_error) are stripped, because the returning job must be
-- indistinguishable from a fresh one.
create function internal.reprocess_dead_letters(
    p_dead_letter_queue text,
    p_origin_queue      text,
    p_limit             integer default 50
)
    returns integer
    language plpgsql
    security definer
    set search_path = pg_catalog, internal
as $$
declare
    v_message record;
    v_count   integer := 0;
begin
    for v_message in
        select msg_id, message
          from pgmq.read(p_dead_letter_queue, 60, p_limit)
    loop
        perform pgmq.send(
            p_origin_queue,
            (v_message.message - 'error_class') - 'last_error'
        );
        perform pgmq.archive(p_dead_letter_queue, v_message.msg_id);
        v_count := v_count + 1;
    end loop;

    return v_count;
end
$$;

-- ---------------------------------------------------------------------------
-- Who runs what
-- ---------------------------------------------------------------------------
revoke execute on function internal.sweep_outbox_unknown() from public;
revoke execute on function internal.correlate_outbox_status(text, text, text) from public;
revoke execute on function internal.review_stale_unknown(interval) from public;
revoke execute on function internal.reprocess_dead_letters(text, text, integer) from public;

-- The sender sweeps its own house.
grant execute on function internal.sweep_outbox_unknown() to sender_role;
grant execute on function internal.review_stale_unknown(interval) to sender_role;

-- Correlation evidence arrives as a status webhook — through ingestion.
grant execute on function internal.correlate_outbox_status(text, text, text) to ingestion_role;

-- Reprocessing is an operator action executed by the runtime (E6 gives it a
-- screen; the grant is the same either way).
grant execute on function internal.reprocess_dead_letters(text, text, integer) to worker_role;

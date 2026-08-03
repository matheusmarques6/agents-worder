-- E1 · the q_domain_events handler — the touch that closes the thread.
--
-- Prova 1 of the milestone: a platform event already ingested becomes ONE
-- outbox row with a fixed text, in a single transaction. This is deliberately
-- NOT the E3 funnel: no suppression, no quota, no staleness ladder, no rate
-- limit, no scheduling. Those arrive with `funnels`/`scheduled_touches` and
-- will run BEFORE this ever writes — the fio only proves the spine exists:
-- event → contact → conversation → outbox.
--
-- Payload contract (decision of this unit): the contact's phone travels in
-- `phone`, E.164 with '+'. The connector Edge Functions (E8) normalise into
-- this shape — exactly as `ingest-meta` normalises `from` for the inbound
-- branch. The function validates the shape itself because failing on the
-- contacts CHECK would roll back the very `failed` mark that makes the
-- problem visible.
--
-- Outcomes are data, not exceptions: `applied`, `already_applied`,
-- `discarded`, `invalid_payload`, `no_channel`. Retrying a data problem never
-- changes its mind, so the event row takes the mark (`failed`/`discarded`)
-- and the job archives with a trail. The one exception that RAISES is a job
-- pointing at an event that does not exist — that is a bug, and bugs take
-- the retry ladder to the DLQ where a human sees them.

create type internal.domain_event_outcome as (
    status          text,
    conversation_id uuid,
    outbox_id       uuid
);

comment on type internal.domain_event_outcome is
    'status: applied | already_applied | discarded | invalid_payload | no_channel. O worker arquiva em todos — desfecho é dado, não exceção.';

create function internal.apply_domain_event(
    p_webhook_event_id bigint,
    p_touch_text       text
)
    returns internal.domain_event_outcome
    language plpgsql
    security definer
    set search_path = pg_catalog, public, internal
as $$
declare
    v_event           internal.webhook_events%rowtype;
    v_phone           text;
    v_channel_id      uuid;
    v_channel_type    text;
    v_contact_id      uuid;
    v_conversation_id uuid;
    v_outbox_id       uuid;
    v_seq             integer;
begin
    select * into v_event
      from internal.webhook_events
     where id = p_webhook_event_id;

    if not found then
        raise exception 'domain event % does not exist', p_webhook_event_id;
    end if;

    -- Redelivery guard. pgmq redelivers for every normal reason (VT expiry,
    -- crash after commit); the second application must be a non-event — one
    -- abandoned cart, one touch.
    if v_event.status = 'processed' then
        return row('already_applied', null, null)::internal.domain_event_outcome;
    end if;

    -- Only the occasions the conversations table knows how to name become
    -- touches. `order_paid` and friends are E3 (they CANCEL funnels there);
    -- today they are discarded with a trail, never failed — reprocessing an
    -- unsupported type would not change the outcome.
    if v_event.event_type not in ('checkout_abandoned', 'cart_abandoned', 'pix_pending') then
        update internal.webhook_events
           set status = 'discarded', processed_at = now()
         where id = p_webhook_event_id;
        return row('discarded', null, null)::internal.domain_event_outcome;
    end if;

    v_phone := v_event.payload ->> 'phone';
    if v_phone is null or v_phone !~ '^\+[1-9][0-9]{7,14}$' then
        update internal.webhook_events
           set status = 'failed', processed_at = now()
         where id = p_webhook_event_id;
        return row('invalid_payload', null, null)::internal.domain_event_outcome;
    end if;

    -- The way OUT for this tenant. Newest active account wins — in E1 there
    -- is exactly one; when routing rules exist they live in tenant config,
    -- not here.
    select id, type
      into v_channel_id, v_channel_type
      from public.channels_accounts
     where tenant_id = v_event.tenant_id
       and status = 'active'
     order by created_at desc
     limit 1;

    if v_channel_id is null then
        update internal.webhook_events
           set status = 'failed', processed_at = now()
         where id = p_webhook_event_id;
        return row('no_channel', null, null)::internal.domain_event_outcome;
    end if;

    -- The touch does NOT bump last_message_at — that timestamp means "the
    -- contact spoke", and it feeds staleness decisions later. A no-op update
    -- is just the price of RETURNING on conflict.
    insert into public.contacts (tenant_id, phone_e164)
    values (v_event.tenant_id, v_phone)
    on conflict (tenant_id, phone_e164)
        do update set phone_e164 = excluded.phone_e164
    returning id into v_contact_id;

    -- An open conversation is reused: a second one would split the history
    -- and the seq counters. Same rule as the inbound branch of ingestion.
    select id
      into v_conversation_id
      from public.conversations
     where tenant_id = v_event.tenant_id
       and contact_id = v_contact_id
       and state <> 'encerrada'
     order by created_at desc
     limit 1;

    if v_conversation_id is null then
        insert into public.conversations
            (tenant_id, contact_id, channel_account_id, origin_occasion)
        values (v_event.tenant_id, v_contact_id, v_channel_id, v_event.event_type)
        returning id into v_conversation_id;
    end if;

    -- The idempotency key derives from the event id, so a redelivered job
    -- could never mint a second key even if the guard above were bypassed —
    -- the outbox UNIQUE is the second lock on the same door.
    insert into internal.message_outbox
        (tenant_id, conversation_id, contact_id, channel_account_id,
         kind, payload, idempotency_key)
    values
        (v_event.tenant_id, v_conversation_id, v_contact_id, v_channel_id,
         'funnel_touch', jsonb_build_object('text', p_touch_text),
         'touch-' || p_webhook_event_id)
    returning id into v_outbox_id;

    v_seq := internal.next_message_seq(v_conversation_id, 'outbound');

    insert into public.messages
        (tenant_id, conversation_id, direction, seq, channel,
         author_type, content, outbox_id)
    values
        (v_event.tenant_id, v_conversation_id, 'outbound', v_seq,
         case v_channel_type when 'cloud' then 'whatsapp_cloud' else 'whatsapp_evolution' end,
         'agent', jsonb_build_object('text', p_touch_text), v_outbox_id);

    update internal.webhook_events
       set status = 'processed', processed_at = now()
     where id = p_webhook_event_id;

    return row('applied', v_conversation_id, v_outbox_id)::internal.domain_event_outcome;
end
$$;

-- ---------------------------------------------------------------------------
-- Who runs it
-- ---------------------------------------------------------------------------
-- SECURITY DEFINER writing across tenants: minimum EXECUTE. The worker is the
-- only consumer of q_domain_events; ingestion produces, the sender speaks, and
-- neither touches this.
revoke execute on function internal.apply_domain_event(bigint, text) from public;
grant execute on function internal.apply_domain_event(bigint, text) to worker_role;

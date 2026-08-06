-- E3 · S3 — the abandonment event becomes the whole cadence.
--
-- The E1 handler (migration 20260803000001) said it out loud in its own first
-- lines: "This is deliberately NOT the E3 funnel... Those arrive with
-- `funnels`/`scheduled_touches` and will run BEFORE this ever writes". They
-- arrive here, and the fixed-text touch is retired with them (decision D6 of
-- `docs/plano-e3-recuperacao.md`).
--
-- The decision this file exists to enforce is **D11**: NOTHING written here
-- reaches `internal.message_outbox`. The original plan sent the first touch
-- straight to the outbox and only scheduled the rest — which would have made
-- the FIRST touch of every funnel skip the protection ladder. RF-033 checks the
-- suppression list before EVERY proactive send and RF-034 counts ALL origins in
-- the 24h window; a fast path that escapes both is the bug the cenário 11 would
-- find late and in production. From here there is exactly one door out: the
-- dispatcher of S4.
--
-- Outcomes stay data, not exceptions, in the mould `apply_domain_event`
-- established: `no_funnel` joins `applied | already_applied | discarded |
-- invalid_payload | no_channel`. The one thing that still RAISES is a job
-- pointing at an event that does not exist — that is a bug, and bugs take the
-- retry ladder to the DLQ where a human sees them.
--
-- Expand-contract: everything here is additive. `apply_domain_event(bigint,
-- text)` keeps its signature and its grants and becomes a shim over the new
-- one-argument function, so the N-1 runtime (which still passes the fixed text)
-- runs against this schema and schedules a funnel instead of sending a touch.
-- The two-argument form is contracted in a later release.

-- ---------------------------------------------------------------------------
-- One abandonment, one cadence — the second lock on the same door
-- ---------------------------------------------------------------------------
-- The redelivery guard on `webhook_events.status` is the first lock, exactly as
-- in E1. This is the second, and it is the same device the outbox's
-- `idempotency_key` was: same funnel, same contact, same touch number, same
-- event instant IS the same abandonment, so a bypassed guard still cannot mint
-- a second cadence. Two abandonments by the same contact differ in `event_at`
-- and coexist freely.
create unique index scheduled_touches_one_per_event
    on public.scheduled_touches (funnel_id, contact_id, touch_number, event_at);

-- ---------------------------------------------------------------------------
-- internal.start_funnel_run — the occasion becomes a schedule
-- ---------------------------------------------------------------------------
create type internal.funnel_run_outcome as (
    status          text,
    funnel_id       uuid,
    conversation_id uuid,
    touches_created integer
);

comment on type internal.funnel_run_outcome is
    'status: started | no_funnel. Desfecho é dado — o chamador arquiva nos dois.';

-- Why this function resolves the contact and the conversation itself, instead
-- of receiving them: `no_funnel` has to write NOTHING. An occasion the merchant
-- did not configure must not leave a person and an empty conversation behind
-- just because the handler walked past. So the funnel lookup comes first, and
-- identity is only created once there is a cadence to attach to it.
--
-- And the conversation is created rather than deferred, even though no message
-- goes out here, because S4's compare-and-set needs it: the ladder's
-- `inbound_seq` guard is `conversations.next_inbound_seq`, and staleness is
-- measured against the newest inbound message in this conversation. A touch
-- scheduled without a conversation would be a touch the ladder cannot protect.
create function internal.start_funnel_run(
    p_tenant_id          uuid,
    p_occasion           text,
    p_phone_e164         text,
    p_channel_account_id uuid,
    p_event_at           timestamptz,
    p_order_id           uuid default null
)
    returns internal.funnel_run_outcome
    language plpgsql
    security definer
    set search_path = pg_catalog, public, internal
as $$
declare
    v_funnel_id       uuid;
    v_touches         jsonb;
    v_max_touches     integer;
    v_contact_id      uuid;
    v_conversation_id uuid;
    v_created         integer;
begin
    -- One enabled funnel per occasion is a unique index (S2), so this cannot
    -- be a coin toss. Scoped by tenant_id in the function itself: SECURITY
    -- DEFINER crosses RLS, so nothing else would stop a neighbour's funnel
    -- from answering this tenant's event.
    select id, touches, max_touches
      into v_funnel_id, v_touches, v_max_touches
      from public.funnels
     where tenant_id = p_tenant_id
       and occasion = p_occasion
       and enabled;

    if not found then
        return row('no_funnel', null, null, 0)::internal.funnel_run_outcome;
    end if;

    -- Same upsert as E1's: the touch does NOT bump `last_message_at`, because
    -- that timestamp means "the contact spoke" and it feeds staleness.
    insert into public.contacts (tenant_id, phone_e164)
    values (p_tenant_id, p_phone_e164)
    on conflict (tenant_id, phone_e164)
        do update set phone_e164 = excluded.phone_e164
    returning id into v_contact_id;

    -- An open conversation is reused: a second one would split the history and
    -- the seq counters, and S4 would guard the wrong `next_inbound_seq`.
    select id
      into v_conversation_id
      from public.conversations
     where tenant_id = p_tenant_id
       and contact_id = v_contact_id
       and state <> 'encerrada'
     order by created_at desc
     limit 1;

    if v_conversation_id is null then
        insert into public.conversations
            (tenant_id, contact_id, channel_account_id, origin_occasion)
        values (p_tenant_id, v_contact_id, p_channel_account_id, p_occasion)
        returning id into v_conversation_id;
    end if;

    -- The cadence, materialised whole.
    --
    -- `due_at = event_at + delay` for EVERY touch, including the first. Two
    -- consequences, both deliberate:
    --
    --   * With the canonical `PT0S` on touch nº 1, `due_at` is the event
    --     instant — already in the past by the time the worker runs, so the
    --     dispatcher picks it up on the next tick (D11). Pinning nº 1 to
    --     `now()` instead would make the first touch the one row whose timing
    --     follows a different rule, and would silently ignore a funnel that
    --     deliberately delays its opening touch.
    --
    --   * A LATE event (queue drained after an outage, reconciliation poll of
    --     S8) materialises a cadence that is already overdue — possibly
    --     several touches of it. That is the honest record of what was
    --     supposed to happen when. Shifting the cadence forward so it "fits"
    --     the current clock would be a scheduling decision taken in the wrong
    --     place, and the only kind that can ADD sends rather than suppress
    --     them. Whether an overdue touch still goes out is the ladder's call
    --     in S4, and RF-034's 24h window is exactly what stops the burst.
    --
    -- `delay` is read as an interval directly: `funnels.touches` carries
    -- ISO-8601 durations (`PT0S`, `PT24H`) and Postgres parses them natively,
    -- so no translation layer can disagree with the configuration. A missing
    -- or malformed delay raises — that is a broken funnel config, and a touch
    -- silently scheduled for "now" would be worse than a loud failure.
    --
    -- `max_touches` is the ceiling of THIS funnel's cadence, never a
    -- per-contact limit: the per-contact protections are the ladder's
    -- (RF-034) and nothing here anticipates them.
    with cadence as (
        select (touch ->> 'n')::integer     as n,
               (touch ->> 'delay')::interval as delay
          from jsonb_array_elements(v_touches) as touch
         order by (touch ->> 'n')::integer
         limit v_max_touches
    )
    insert into public.scheduled_touches
        (tenant_id, funnel_id, contact_id, conversation_id, order_id,
         touch_number, event_at, due_at)
    select p_tenant_id, v_funnel_id, v_contact_id, v_conversation_id, p_order_id,
           n, p_event_at, p_event_at + delay
      from cadence;

    get diagnostics v_created = row_count;

    return row('started', v_funnel_id, v_conversation_id, v_created)::internal.funnel_run_outcome;
end
$$;

comment on function internal.start_funnel_run(uuid, text, text, uuid, timestamptz, uuid) is
    'D11: escreve SÓ em scheduled_touches. Nenhum toque vai direto para a outbox — '
    'a escada do S4 é a única porta de saída de todo envio proativo.';

-- ---------------------------------------------------------------------------
-- internal.apply_domain_event — the E1 handler, now routing to the funnel
-- ---------------------------------------------------------------------------
-- The order of the outcomes is load-bearing and unchanged where it matters:
-- `already_applied` before anything (redelivery is not an event), then the
-- unsupported type, then the payload, then the way out, and only then the
-- funnel. `no_funnel` deliberately sits AFTER `no_channel`: a tenant with no
-- active number has a connection problem a human must fix, while a tenant with
-- no funnel has made a choice — reporting the choice would hide the fault.
create function internal.apply_domain_event(p_webhook_event_id bigint)
    returns internal.domain_event_outcome
    language plpgsql
    security definer
    set search_path = pg_catalog, public, internal
as $$
declare
    v_event      internal.webhook_events%rowtype;
    v_phone      text;
    v_channel_id uuid;
    v_run        internal.funnel_run_outcome;
begin
    select * into v_event
      from internal.webhook_events
     where id = p_webhook_event_id;

    if not found then
        raise exception 'domain event % does not exist', p_webhook_event_id;
    end if;

    -- Redelivery guard. pgmq redelivers for every normal reason (VT expiry,
    -- crash after commit); the second application must be a non-event — one
    -- abandoned cart, one funnel.
    if v_event.status = 'processed' then
        return row('already_applied', null, null)::internal.domain_event_outcome;
    end if;

    -- Only the occasions a funnel can have. `order_paid` CANCELS funnels, and
    -- that handler is S5; until then it is discarded with a trail, never
    -- failed — reprocessing an unsupported type would not change the outcome.
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

    -- The way OUT for this tenant. Newest active account wins; when routing
    -- rules exist (`funnels.channel_preference`, S7) they live in config.
    select id
      into v_channel_id
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

    -- `received_at`, never `now()`. That is the whole reason
    -- `scheduled_touches.event_at` was created without a default (S2): the
    -- instant the platform's fact reached us is what staleness compares a
    -- newer message against, and substituting the scheduling instant would
    -- make a contact who replied WHILE the job waited in the queue look
    -- silent. When the connectors carry a platform timestamp (E8) and the
    -- reconciliation poll of S8 ingests events long after they happened, a
    -- better `event_at` is an additive change to this one line.
    select * into v_run
      from internal.start_funnel_run(
               v_event.tenant_id,
               v_event.event_type,
               v_phone,
               v_channel_id,
               v_event.received_at);

    if v_run.status = 'no_funnel' then
        -- A trail, not a failure: the merchant has this occasion switched off
        -- (or never configured), and reprocessing would not change that.
        update internal.webhook_events
           set status = 'discarded', processed_at = now()
         where id = p_webhook_event_id;
        return row('no_funnel', null, null)::internal.domain_event_outcome;
    end if;

    update internal.webhook_events
       set status = 'processed', processed_at = now()
     where id = p_webhook_event_id;

    -- The third field stays null and that is the point of the milestone: an
    -- applied event no longer produces an outbox row. The column survives
    -- because the type is shared with the N-1 runtime (expand-contract).
    return row('applied', v_run.conversation_id, null)::internal.domain_event_outcome;
end
$$;

comment on type internal.domain_event_outcome is
    'status: applied | already_applied | discarded | invalid_payload | no_channel | no_funnel. '
    'O worker arquiva em todos — desfecho é dado, não exceção. Desde o E3 S3 `applied` significa '
    '"a cadência do funil existe", e outbox_id é sempre nulo (D11).';

-- The N-1 shim. The previous runtime still calls this with the retired fixed
-- text; it must keep working against the expanded schema (the N-1
-- compatibility rule), and what it now does is schedule a funnel. The text is
-- ignored rather than removed, because removing the parameter would break the
-- older image mid-deploy — the contract phase drops it in a later release.
create or replace function internal.apply_domain_event(
    p_webhook_event_id bigint,
    p_touch_text       text
)
    returns internal.domain_event_outcome
    language plpgsql
    security definer
    set search_path = pg_catalog, public, internal
as $$
declare
    v_outcome internal.domain_event_outcome;
begin
    select * into v_outcome from internal.apply_domain_event(p_webhook_event_id);
    return v_outcome;
end
$$;

comment on function internal.apply_domain_event(bigint, text) is
    'Shim N-1 (expand-contract): o texto do toque fixo do E1 é ignorado desde o E3 S3 (D6). '
    'Removido na fase de contração.';

-- ---------------------------------------------------------------------------
-- Who runs it
-- ---------------------------------------------------------------------------
-- SECURITY DEFINER writing across tenants: minimum EXECUTE (ADR-11). The worker
-- is the only consumer of q_domain_events; ingestion produces, the sender
-- speaks, and neither has any business starting a funnel.
revoke execute on function internal.apply_domain_event(bigint) from public;
grant execute on function internal.apply_domain_event(bigint) to worker_role;

revoke execute on function internal.start_funnel_run(uuid, text, text, uuid, timestamptz, uuid)
    from public;
grant execute on function internal.start_funnel_run(uuid, text, text, uuid, timestamptz, uuid)
    to worker_role;

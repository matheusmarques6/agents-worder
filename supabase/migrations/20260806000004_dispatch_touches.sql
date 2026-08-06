-- E3 · S4 — the due touch becomes a send, or dies with a reason.
--
-- S3 made every proactive touch start life as a row in `scheduled_touches`
-- (D11). This migration builds the only door out of that table, and it is built
-- as **two** functions rather than one on purpose:
--
--   `internal.claim_due_touches`  — cross-tenant, SECURITY DEFINER, the mould of
--   `claim_outbox_batch`: a scan of everything that is due, `FOR UPDATE SKIP
--   LOCKED`, marking what it takes and returning only what it took. Like the
--   sender's claim it has **no filter parameter** — a caller that could ask for
--   "only tenant X" would be an arbitrary cross-tenant query under another name
--   (ADR-11).
--
--   `internal.dispatch_touch` — per touch, SECURITY INVOKER, RLS in the path,
--   and the compare-and-set of **D2**. The ladder (`dispatch/ladder.py`) has
--   already decided, in Python, outside every transaction; this re-asserts the
--   facts it stood on inside the same short transaction as the write. A fact
--   that moved in between (a message arrived, the order was paid, the contact
--   was blocked, another touch went out) fails the `WHERE`, and NOTHING is
--   sent.
--
-- The rule is not duplicated here, and the effort to keep it that way is
-- visible in the signature: the three windows the decision used
-- (`PROACTIVE_WINDOW`, `FUNNEL_COOLDOWN`, `TIER_PAUSE_FRACTION`) and the
-- effective per-contact cap arrive as PARAMETERS. Writing `interval '24 hours'`
-- here would be a second copy of a canonical number, and the day somebody
-- changed the one in `ladder.py` the revalidation would silently enforce the
-- old one. What this file contains is the query, never the threshold.
--
-- Why two statements and not one clever UPDATE: PostgreSQL's READ COMMITTED
-- re-check (EvalPlanQual) re-evaluates the qual of a blocked UPDATE against the
-- newly committed row, but the SUBQUERIES in that qual keep the command's
-- original snapshot — "it can see the effects of concurrent updating commands
-- on the same rows it is trying to update, but it does not see effects of those
-- commands on other rows". A message that landed while we waited lives on
-- another row. So `dispatch_touch` takes the touch with `FOR UPDATE` first, as
-- its own statement, and only then runs the CAS: a new statement takes a new
-- snapshot, and every guard is re-read against the world as it is now. That is
-- the difference between a CAS that looks right and one that holds.

-- ---------------------------------------------------------------------------
-- Who took a touch, and when
-- ---------------------------------------------------------------------------
-- Additive, and not decoration: an `enqueued` touch that never became anything
-- is the failure mode of this step, and "which pass claimed it, at what time"
-- is the whole diagnosis. Without them `p_worker_id` would be a parameter the
-- function accepts and forgets.
alter table public.scheduled_touches
    add column claimed_by uuid,
    add column claimed_at timestamptz;

comment on column public.scheduled_touches.claimed_by is
    'O passe do dispatcher que reivindicou este toque. Diagnóstico de toque parado em enqueued.';

-- ---------------------------------------------------------------------------
-- `funnels.touches` stops being free-form jsonb — dívida (b) do S3
-- ---------------------------------------------------------------------------
-- Today a merchant can save a cadence with no `delay`, with the same `n` twice,
-- or with nothing in it at all. The first raises inside `start_funnel_run` at
-- 3am, the second schedules two touches the dispatcher cannot tell apart, and
-- the third is a funnel that is switched on and silently does nothing — the
-- worst of the three, because there is no error to find.
--
-- Marked IMMUTABLE and meant literally: every operator in here is immutable
-- (jsonb traversal, text regex, text→numeric). The ISO-8601 duration is checked
-- as TEXT rather than by casting to `interval`, because that cast is only
-- STABLE (it reads DateStyle) and a CHECK constraint may not depend on a
-- setting somebody can change per session.
create function public.funnel_cadence_is_valid(p_touches jsonb)
    returns boolean
    language sql
    immutable
    set search_path = pg_catalog
as $$
    select jsonb_typeof(p_touches) = 'array'
       -- An empty cadence is a funnel that is enabled and mute. `enabled` is
       -- the switch; an empty list is an accident.
       and jsonb_array_length(p_touches) > 0
       and not exists (
           select 1
             from jsonb_array_elements(p_touches) as touch
            where jsonb_typeof(touch) <> 'object'
               -- `n` orders the cadence and identifies the touch in
               -- `scheduled_touches.touch_number`, which is `integer > 0`.
               or jsonb_typeof(touch -> 'n') <> 'number'
               or (touch ->> 'n')::numeric <> trunc((touch ->> 'n')::numeric)
               or (touch ->> 'n')::numeric < 1
               -- ISO-8601 duration: `PT0S`, `PT24H`, `P1DT6H`. `start_funnel_run`
               -- casts this straight to `interval`, so a shape Postgres cannot
               -- parse is a cadence that explodes at scheduling time.
               or coalesce(touch ->> 'delay', '') !~ '^P([0-9]+[YMWD])*(T([0-9]+[HMS])+)?$'
               or touch ->> 'delay' = 'P'
               -- Text is the one thing every adapter can deliver today —
               -- `channels/cloud_api.py` rejects an outbox payload without it.
               -- A cadence entry naming only a template is a touch nothing can
               -- send; when template sending exists (S7) this loosens, and
               -- loosening a CHECK is additive.
               or coalesce(touch ->> 'copy_base', '') = ''
       )
       -- Duplicate `n`: two touches the dispatcher cannot tell apart, and a
       -- unique index (`scheduled_touches_one_per_event`) that would reject the
       -- second half of the cadence at insert time.
       and (
           select count(distinct touch ->> 'n') = count(*)
             from jsonb_array_elements(p_touches) as touch
       )
$$;

comment on function public.funnel_cadence_is_valid(jsonb) is
    'Forma da cadência (R5 do plano E3): lista não vazia de {n, delay, copy_base[, template_ref, cta]}, '
    'n inteiro positivo e distinto, delay em ISO-8601.';

-- The default goes with it. `'[]'` was the one value the constraint now
-- rejects, so leaving it would turn "I forgot the cadence" into a confusing
-- CHECK violation on a column nobody mentioned; without a default it is a NOT
-- NULL violation naming `touches`, which is the truth.
alter table public.funnels
    alter column touches drop default,
    add constraint funnels_touches_is_a_cadence
        check (public.funnel_cadence_is_valid(touches));

-- ---------------------------------------------------------------------------
-- internal.claim_due_touches — the scan, every minute
-- ---------------------------------------------------------------------------
create type internal.claimed_touch as (
    scheduled_touch_id uuid,
    tenant_id          uuid
);

comment on type internal.claimed_touch is
    'Ids only, a regra do desfecho como dado: o snapshot é carregado pelo handler '
    'sob a RLS do tenant, e uma cópia dentro do payload é uma cópia que diverge.';

-- SECURITY DEFINER for the same reason as `claim_outbox_batch` and the
-- coalescer: the sweep is cross-tenant by nature and no application role may
-- hold a global SELECT (ADR-11). `SKIP LOCKED` is what lets a second dispatcher
-- pass — or a second process, the day there is one — take a disjoint batch
-- instead of waiting.
--
-- Marking the rows `enqueued` inside the same statement is what makes the claim
-- a claim: the next pass, one minute later, sees `status = 'pending'` and does
-- not hand the same touch to a second job.
create function internal.claim_due_touches(
    p_worker_id uuid,
    p_limit     integer default 100
)
    returns setof internal.claimed_touch
    language sql
    security definer
    set search_path = pg_catalog, public, internal
as $$
    with due as (
        select id
          from public.scheduled_touches
         where status = 'pending'
           and due_at <= now()
         order by due_at
         for update skip locked
         limit p_limit
    ),
    marked as (
        update public.scheduled_touches t
           set status = 'enqueued',
               claimed_by = p_worker_id,
               claimed_at = now()
          from due
         where t.id = due.id
        returning t.id, t.tenant_id
    )
    select id, tenant_id from marked
$$;

comment on function internal.claim_due_touches(uuid, integer) is
    'Varredura cross-tenant dos toques vencidos, no molde de claim_outbox_batch. '
    'Sem parâmetro de filtro: pedir "só o tenant X" seria consulta cross-tenant arbitrária.';

-- ---------------------------------------------------------------------------
-- internal.dispatch_touch — o CAS de gravação (D2)
-- ---------------------------------------------------------------------------
create type internal.touch_dispatch as (
    -- sent     — the guards still hold; the outbox row exists and so does the
    --            message, in the same transaction as the touch's `sent`.
    -- conflict — a guard moved. NOTHING was written. The caller reloads the
    --            snapshot, runs the ladder again and cancels with ITS reason —
    --            naming the reason here would be the rule written twice.
    -- gone     — the touch is no longer `enqueued`: another job finished it, or
    --            `order_paid` cancelled it (S5). Not an error, not a conflict.
    -- no_channel — the touch has no conversation, or its conversation has no
    --            way out. Neither a guard nor a cancellation: a broken
    --            arrangement, and the caller raises so it reaches a human
    --            through the DLQ instead of being filed as a protection.
    status    text,
    outbox_id uuid
);

comment on type internal.touch_dispatch is
    'status: sent | conflict | gone | no_channel. conflict significa que um fato mudou entre decidir '
    'e gravar e NADA foi escrito — quem chamou nomeia o motivo rodando a escada de novo.';

create function internal.dispatch_touch(
    p_touch_id              uuid,
    -- The guard values the DECISION used. The cap is the effective one, already
    -- clamped to the platform ceiling by the ladder: revalidating against the
    -- raw tenant value would be laxer than the rule that allowed the touch.
    p_inbound_seq           integer,
    p_max_proactive_per_24h integer,
    -- The canonical windows, from `dispatch/ladder.py`. Parameters, not
    -- literals: one copy of a canonical number is a rule, two are a future
    -- disagreement nobody notices.
    p_proactive_window      interval,
    p_funnel_cooldown       interval,
    p_tier_pause_fraction   double precision,
    p_payload               jsonb,
    p_idempotency_key       text
)
    returns internal.touch_dispatch
    language plpgsql
    -- INVOKER: one touch belongs to one tenant, the worker is already scoped by
    -- `SET LOCAL app.tenant_id`, and RLS staying in the path is the point. Only
    -- the claim above needs to cross tenants, and only the claim is DEFINER.
    set search_path = pg_catalog, public, internal
as $$
declare
    v_touch      public.scheduled_touches%rowtype;
    v_channel_id uuid;
    v_channel    text;
    v_outbox_id  uuid;
    v_seq        integer;
begin
    -- Statement one: take the row. This is the serialisation point, and the
    -- reason the CAS below runs under a snapshot that includes everything
    -- committed while we waited here (see the header).
    select * into v_touch
      from public.scheduled_touches
     where id = p_touch_id
       and status = 'enqueued'
       for update;

    if not found then
        return row('gone', null)::internal.touch_dispatch;
    end if;

    select c.channel_account_id,
           case ca.type when 'cloud' then 'whatsapp_cloud' else 'whatsapp_evolution' end
      into v_channel_id, v_channel
      from public.conversations c
      join public.channels_accounts ca on ca.id = c.channel_account_id
     where c.id = v_touch.conversation_id;

    if v_channel_id is null then
        -- A touch with no way out is not a conflict and not a cancellation: it
        -- is a broken arrangement, and the caller raises so a human sees it.
        return row('no_channel', null)::internal.touch_dispatch;
    end if;

    -- Statement two: THE compare-and-set. Every conjunct re-asserts one fact
    -- the ladder's decision stood on; each is annotated with the guard it
    -- revalidates and the reason the ladder gives when it is the one that moved.
    update public.scheduled_touches t
       set status = 'sent',
           sent_at = now()
     where t.id = p_touch_id
       -- Still ours to finish (Guards: none — this is the claim itself).
       and t.status = 'enqueued'
       -- Guards.suppression_absent → suppressed_block | _silence | _optout.
       -- Recomputed rather than compared: a row appearing in between can only
       -- make the predicate stricter (RF-033).
       and not exists (
           select 1 from public.suppression_list s
            where s.tenant_id = t.tenant_id
              and s.contact_id = t.contact_id
       )
       -- Guards.order_unpaid → stale_order_paid. Charging somebody who already
       -- paid is the single worst thing this product can do.
       and not exists (
           select 1 from public.orders o
            where o.id = t.order_id
              and o.financial_status = 'paid'
       )
       -- Guards.inbound_seq → stale_newer_message. The atomic counter, the same
       -- device as the central invariant's CAS in E1: a message that arrived
       -- while we decided bumped it, and equality fails.
       and exists (
           select 1 from public.conversations c
            where c.id = t.conversation_id
              and c.next_inbound_seq = p_inbound_seq
       )
       -- The same fact by its other name, and deliberately kept alongside the
       -- counter: the ladder measures staleness as "an inbound message newer
       -- than the event", and the conjunct that enforces it should be the one
       -- the rule is written in. The counter catches an ingestion that is
       -- mid-flight; this catches a message whose row exists.
       and not exists (
           select 1 from public.messages m
            where m.conversation_id = t.conversation_id
              and m.direction = 'inbound'
              and m.created_at > t.event_at
       )
       -- Guards.proactive_sends_24h / max_proactive_per_24h → rate_limit_24h.
       -- The outbox, by `kind` and by `created_at`: RF-034 sums ALL origins,
       -- and the commitment counts, not the delivery (S2 fixed both, and
       -- `message_outbox_proactive_contact_idx` is the index for exactly this).
       and (
           select count(*) from internal.message_outbox o
            where o.contact_id = t.contact_id
              and o.kind in ('funnel_touch', 'followup')
              and o.created_at > now() - p_proactive_window
       ) < p_max_proactive_per_24h
       -- Guards.other_funnel_touch_at → funnel_cooldown_72h. Between DIFFERENT
       -- funnels only: inside one funnel the spacing is the funnel's cadence.
       and not exists (
           select 1 from public.scheduled_touches other
            where other.contact_id = t.contact_id
              and other.funnel_id <> t.funnel_id
              and other.status = 'sent'
              and other.sent_at > now() - p_funnel_cooldown
       )
       -- Guards.tier_limit_24h / tier_usage_24h → channel_paused_tier. NULL
       -- tier is Evolution, where the ceilings are the sender's anti-ban ones.
       and not exists (
           select 1
             from public.conversations c
             join public.channels_accounts ca on ca.id = c.channel_account_id
            where c.id = t.conversation_id
              and ca.meta_tier is not null
              and ca.tier_usage_24h >= p_tier_pause_fraction * ca.meta_tier
       );

    if not found then
        -- Nothing was written. The touch stays `enqueued` and the caller
        -- decides what it is called.
        return row('conflict', null)::internal.touch_dispatch;
    end if;

    -- From here down it commits with the CAS above or not at all. A touch
    -- marked `sent` with no outbox row would be the outbound version of "the
    -- deadline passed and no job exists": the cooldown counts it, the contact
    -- never hears it, and nothing anywhere says so.
    insert into internal.message_outbox
        (tenant_id, conversation_id, contact_id, channel_account_id,
         kind, payload, idempotency_key)
    values
        (v_touch.tenant_id, v_touch.conversation_id, v_touch.contact_id, v_channel_id,
         'funnel_touch', p_payload, p_idempotency_key)
    returning id into v_outbox_id;

    v_seq := internal.next_message_seq(v_touch.conversation_id, 'outbound');

    -- The conversation has to contain what the store said. Without this row the
    -- agent that answers the contact's reply (E2) reads a history where it
    -- spoke first about nothing.
    insert into public.messages
        (tenant_id, conversation_id, direction, seq, channel,
         author_type, content, outbox_id)
    values
        (v_touch.tenant_id, v_touch.conversation_id, 'outbound', v_seq, v_channel,
         'agent', p_payload, v_outbox_id);

    update public.scheduled_touches
       set outbox_id = v_outbox_id
     where id = p_touch_id;

    return row('sent', v_outbox_id)::internal.touch_dispatch;
end
$$;

comment on function internal.dispatch_touch(uuid, integer, integer, interval, interval, double precision, jsonb, text) is
    'D2: a escada decide fora da transação, este WHERE revalida cada guard dentro dela. '
    'Fato mudou entre decidir e gravar → conflict, e nada sai.';

-- ---------------------------------------------------------------------------
-- internal.cancel_touch — o toque que não vai existir, com o motivo
-- ---------------------------------------------------------------------------
-- The reason is the caller's word, not this function's: the vocabulary belongs
-- to `dispatch/ladder.py` and the CHECK on `cancel_reason` is what keeps the
-- two honest. `manual` reaches here from the operator's path (E5).
create function internal.cancel_touch(p_touch_id uuid, p_reason text)
    returns boolean
    language plpgsql
    set search_path = pg_catalog, public
as $$
begin
    update public.scheduled_touches
       set status = 'cancelled',
           cancel_reason = p_reason
     where id = p_touch_id
       -- Never a touch that already went out: cancelling a `sent` touch would
       -- erase it from the 72h cooldown and from the conversion window.
       and status in ('pending', 'enqueued');

    return found;
end
$$;

-- ---------------------------------------------------------------------------
-- Quem executa
-- ---------------------------------------------------------------------------
revoke execute on function internal.claim_due_touches(uuid, integer) from public;
revoke execute on function
    internal.dispatch_touch(uuid, integer, integer, interval, interval, double precision, jsonb, text)
    from public;
revoke execute on function internal.cancel_touch(uuid, text) from public;

-- The worker, and nobody else. `sender_role` drains what this produces; it has
-- no business producing one.
grant execute on function internal.claim_due_touches(uuid, integer) to worker_role;
grant execute on function
    internal.dispatch_touch(uuid, integer, integer, interval, interval, double precision, jsonb, text)
    to worker_role;
grant execute on function internal.cancel_touch(uuid, text) to worker_role;

comment on table pgmq.q_q_scheduled is
    'Toques vencidos, reivindicados por internal.claim_due_touches. '
    'Payload {scheduled_touch_id, tenant_id}.';

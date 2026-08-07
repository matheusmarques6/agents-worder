-- E3 · S5 — the payment cancels the funnel, and credits what it recovered.
--
-- The E1 handler said it in its own first lines: "`order_paid` and friends are
-- E3 (they CANCEL funnels there); today they are discarded with a trail". They
-- arrive here, and with them the other half of the product: until now the
-- milestone could only chase somebody, never notice that they paid.
--
-- Three effects, one transaction, and each one is a decision of
-- `docs/plano-e3-recuperacao.md`:
--
--   D4 — **the mirror.** `orders` and `customers` are upserted by
--   `(connector_account_id, external_id)`. This is the slice of RF-070 the
--   milestone needs — payment and context — never a full platform sync, which
--   is E5's job and has its own screens.
--
--   D7 — **the immediate cancellation.** The payment kills the contact's open
--   touches right here, in the domain handler, with
--   `cancel_reason = 'stale_order_paid'`: the ladder's own word, never a
--   synonym, because two values for one fact break the "cancelled BY REASON"
--   metric of S11 exactly as flattening nine reasons into four would. It is
--   the contact's money and the age promotion of ADR-4 exists for precisely
--   this event. The ladder keeps revalidating at dispatch — defence in depth,
--   not redundancy: `internal.dispatch_touch`'s `WHERE` still refuses a touch
--   whose order was paid between the decision and the write.
--
--   D8 — **attribution is a recorded fact.** A touch SENT inside
--   `tenants.attribution_window_hours` before the payment becomes a row in
--   `funnel_conversions`, with the value and the currency COPIED rather than
--   joined. Recalculating later is impossible: `messages` has a rolling TTL of
--   12 months and the contact can be purged in E6, while the recovered-revenue
--   number has to survive both.
--
-- Payload contract (decision of this unit, in the mould of E1's `phone`): the
-- order travels in `order`, whose `external_id` is the only required field; the
-- contact's phone travels in `phone`, E.164 with '+', and the customer in
-- `order.customer`. Everything else is taken only when it is well formed —
-- refusing a payment over a malformed tracking code would keep chasing somebody
-- who already paid, which is the single worst thing this product can do.
--
-- Expand-contract: every change is additive. `orders` gains a nullable column,
-- `apply_domain_event` gains a branch, and the N-1 runtime — which calls the
-- two-argument shim and knows nothing about payments — keeps working unchanged.

-- ---------------------------------------------------------------------------
-- orders.contact_id — the additive fix the S2 finding authorised
-- ---------------------------------------------------------------------------
-- The path from an order to a person was three text hops
-- (`orders.customer_external_id` → `customers.external_id` →
-- `customers.phone_e164` → `contacts.phone_e164`), and the problem with it is
-- not speed, it is failure mode: two of those columns are NULLABLE and one is
-- the platform's own formatting of a phone number. A join across them does not
-- fail when the mirror is incomplete, it returns "no contact" — and "no
-- contact" is indistinguishable from "we never messaged this person", which is
-- precisely the case where this handler must do nothing. A wrong answer that
-- looks like a legitimate answer is the shape of bug nobody finds.
--
-- So the link is recorded at the one instant it is certainly known: the mirror
-- is written by the same statement that read the phone out of the event. First
-- writer wins on update, so a later event with no phone never erases it.
alter table public.orders
    add column contact_id uuid references public.contacts (id) on delete set null;

create index orders_tenant_contact_idx on public.orders (tenant_id, contact_id)
    where contact_id is not null;

comment on column public.orders.contact_id is
    'Quem é a pessoa deste pedido, gravado no espelho (S5) em vez de deduzido por '
    'três saltos de texto. ON DELETE SET NULL: a purga do contato não apaga o pedido.';

-- ---------------------------------------------------------------------------
-- internal.mirror_order — o espelho, com dois escritores
-- ---------------------------------------------------------------------------
-- Called by both branches of the domain handler: the abandonment (which says
-- nothing about payment, so it passes `p_financial_status = null`) and the
-- payment (which passes `'paid'`). One mirror with two writers, never two
-- mirrors — a second write path would be a second idempotency to keep in step,
-- which is the reasoning D5 gives for the reconciliation poll.
--
-- SECURITY INVOKER: one order belongs to one tenant, and when the worker calls
-- it directly RLS stays in the path. It is invoked from inside a SECURITY
-- DEFINER function today, so those writes run as the definer — the same as
-- every other statement in that handler.
create function internal.mirror_order(
    p_tenant_id            uuid,
    p_connector_account_id uuid,
    p_order                jsonb,
    -- The contact this order belongs to, when the event knew one. NULL is not a
    -- failure: an order can be mirrored for somebody we have never messaged.
    p_contact_id           uuid,
    -- NULL means "this event makes no claim about payment". It is what keeps an
    -- abandonment from pushing a paid order back to `pending` and resurrecting a
    -- funnel target the payment had already disarmed.
    p_financial_status     text,
    -- The instant the platform's fact reached us, never `now()` — the same rule
    -- `scheduled_touches.event_at` was created without a default for.
    p_seen_at              timestamptz
)
    returns uuid
    language plpgsql
    set search_path = pg_catalog, public, internal
as $$
declare
    v_external          text := nullif(p_order ->> 'external_id', '');
    v_customer          jsonb := p_order -> 'customer';
    v_customer_external text;
    v_customer_phone    text;
    v_customer_id       uuid;
    v_total             numeric(12, 2);
    v_currency          text;
    v_items             jsonb;
    v_order_id          uuid;
begin
    -- The identity of the order is the one thing that cannot be missing: the
    -- mirror is keyed on it, and a row we cannot key is a row we can never
    -- update again. The caller decides what to call that.
    if v_external is null then
        return null;
    end if;

    v_customer_external := coalesce(
        nullif(v_customer ->> 'external_id', ''),
        nullif(p_order ->> 'customer_external_id', '')
    );

    -- Decorative fields, taken only when well formed. The alternative — casting
    -- whatever arrived — turns a platform's odd total into an exception that
    -- rolls back the whole payment, including the cancellation that stops us
    -- charging somebody who already paid.
    v_total := case
                   when p_order ->> 'total' ~ '^[0-9]+(\.[0-9]+)?$'
                   then (p_order ->> 'total')::numeric(12, 2)
               end;
    v_currency := case
                      when p_order ->> 'currency' ~ '^[A-Za-z]{3}$'
                      then upper(p_order ->> 'currency')
                  end;
    v_items := case
                   when jsonb_typeof(p_order -> 'items') = 'array'
                   then p_order -> 'items'
                   else '[]'::jsonb
               end;

    if v_customer_external is not null then
        v_customer_phone := nullif(v_customer ->> 'phone', '');
        if v_customer_phone !~ '^\+[1-9][0-9]{7,14}$' then
            -- Same shape as `contacts.phone_e164` or nothing at all: a phone
            -- stored in two shapes is two people who are one person.
            v_customer_phone := null;
        end if;

        -- `total_orders`, `total_spent` and `avg_ticket` are deliberately left
        -- at their defaults. They are aggregates of the whole order book, and
        -- deriving them from the events that happen to arrive would produce a
        -- number that is confidently wrong. The full sync (RF-070) owns them.
        insert into public.customers
            (tenant_id, connector_account_id, external_id, name, email, phone_e164,
             first_order_at, last_order_at, synced_at)
        values
            (p_tenant_id, p_connector_account_id, v_customer_external,
             nullif(v_customer ->> 'name', ''), nullif(v_customer ->> 'email', ''),
             v_customer_phone, p_seen_at, p_seen_at, now())
        on conflict (connector_account_id, external_id) do update
            set name           = coalesce(excluded.name, customers.name),
                email          = coalesce(excluded.email, customers.email),
                phone_e164     = coalesce(excluded.phone_e164, customers.phone_e164),
                first_order_at = least(coalesce(customers.first_order_at, excluded.first_order_at),
                                       excluded.first_order_at),
                last_order_at  = greatest(coalesce(customers.last_order_at, excluded.last_order_at),
                                          excluded.last_order_at),
                synced_at      = now()
        returning id into v_customer_id;

        -- `contacts.customer_id` was added in S2 (the omission of the E1 slice)
        -- and until now nothing wrote it. A column with no writer is a column
        -- that lies to whoever reads it first — the prompt's `customer_context`
        -- in S9, or the hub in E5.
        if p_contact_id is not null then
            update public.contacts
               set customer_id = v_customer_id
             where id = p_contact_id
               and customer_id is distinct from v_customer_id;
        end if;
    end if;

    insert into public.orders
        (tenant_id, connector_account_id, external_id, customer_external_id, contact_id,
         status, financial_status, total, currency, items,
         tracking_code, tracking_status, synced_at)
    values
        (p_tenant_id, p_connector_account_id, v_external, v_customer_external, p_contact_id,
         nullif(p_order ->> 'status', ''), coalesce(p_financial_status, 'pending'),
         v_total, coalesce(v_currency, 'BRL'), v_items,
         nullif(p_order ->> 'tracking_code', ''), nullif(p_order ->> 'tracking_status', ''),
         now())
    on conflict (connector_account_id, external_id) do update
        set customer_external_id = coalesce(excluded.customer_external_id,
                                            orders.customer_external_id),
            -- First writer wins: a later event that carries no phone must not
            -- erase a link we already established.
            contact_id           = coalesce(orders.contact_id, excluded.contact_id),
            status               = coalesce(excluded.status, orders.status),
            financial_status     = coalesce(p_financial_status, orders.financial_status),
            total                = coalesce(v_total, orders.total),
            currency             = coalesce(v_currency, orders.currency),
            items                = case when jsonb_array_length(excluded.items) > 0
                                        then excluded.items else orders.items end,
            tracking_code        = coalesce(excluded.tracking_code, orders.tracking_code),
            tracking_status      = coalesce(excluded.tracking_status, orders.tracking_status),
            synced_at            = now()
    returning id into v_order_id;

    return v_order_id;
end
$$;

comment on function internal.mirror_order(uuid, uuid, jsonb, uuid, text, timestamptz) is
    'O espelho do RF-070 na fatia do E3 (D4): upsert por (connector_account_id, external_id). '
    'p_financial_status nulo = este evento não afirma nada sobre pagamento.';

-- ---------------------------------------------------------------------------
-- internal.apply_order_paid — o pagamento, aplicado
-- ---------------------------------------------------------------------------
create function internal.apply_order_paid(p_webhook_event_id bigint)
    returns internal.domain_event_outcome
    language plpgsql
    security definer
    set search_path = pg_catalog, public, internal
as $$
declare
    v_event      internal.webhook_events%rowtype;
    v_order      jsonb;
    v_connector  uuid;
    v_phone      text;
    v_contact_id uuid;
    v_order_id   uuid;
    v_paid_at    timestamptz;
    v_window     smallint;
    v_touch_id   uuid;
    v_funnel_id  uuid;
begin
    select * into v_event
      from internal.webhook_events
     where id = p_webhook_event_id;

    if not found then
        raise exception 'domain event % does not exist', p_webhook_event_id;
    end if;

    v_order := v_event.payload -> 'order';
    if v_order is null
       or jsonb_typeof(v_order) <> 'object'
       or coalesce(v_order ->> 'external_id', '') = ''
    then
        -- A payment that does not say WHICH order is the connector adapter
        -- broken, and that a human fixes. `failed` rather than `discarded`
        -- because reprocessing after the fix does resolve it.
        update internal.webhook_events
           set status = 'failed', processed_at = now()
         where id = p_webhook_event_id;
        return row('invalid_payload', null, null)::internal.domain_event_outcome;
    end if;

    -- The store this order belongs to. `ingest_webhook` resolved the tenant
    -- through this very row, so in production it exists; when it does not, the
    -- store was disconnected between ingestion and processing.
    select id
      into v_connector
      from public.connector_accounts
     where tenant_id = v_event.tenant_id
       and platform = v_event.source
       and source_account_id = v_event.source_account_id;

    if v_connector is null then
        -- Its own outcome, not `invalid_payload`: the payload was perfect and
        -- the ARRANGEMENT is what vanished, so the fix is different (reconnect
        -- the store, not correct the adapter) and the S11 bucket is different
        -- too. Same shape as E1's `no_channel`, mark included — a paid order the
        -- mirror loses is exactly what this milestone exists not to lose.
        update internal.webhook_events
           set status = 'failed', processed_at = now()
         where id = p_webhook_event_id;
        return row('no_store', null, null)::internal.domain_event_outcome;
    end if;

    -- The payment instant. `received_at`, never `now()`: a job drained hours
    -- after an outage must credit the revenue to the day the money arrived and
    -- must not count a touch that went out after it. When the connectors carry
    -- a platform timestamp (E8), a better instant is an additive change to this
    -- one line.
    v_paid_at := v_event.received_at;

    v_phone := coalesce(
        nullif(v_event.payload ->> 'phone', ''),
        nullif(v_order -> 'customer' ->> 'phone', '')
    );
    if v_phone is not null and v_phone ~ '^\+[1-9][0-9]{7,14}$' then
        -- Looked up, never upserted. A payment is not permission to message
        -- somebody: creating the contact here would plant a person no
        -- conversation justifies, and the suppression list of S6 would then
        -- have to protect them from us.
        select id
          into v_contact_id
          from public.contacts
         where tenant_id = v_event.tenant_id
           and phone_e164 = v_phone;
    end if;

    v_order_id := internal.mirror_order(
        v_event.tenant_id, v_connector, v_order, v_contact_id, 'paid', v_paid_at);

    if v_contact_id is not null then
        -- D7. `pending` and `enqueued` only: a touch already `sent` is never
        -- cancelled, for the reason `internal.cancel_touch` gives — erasing it
        -- would take it out of the 72h cooldown and out of the conversion
        -- window it may itself have earned.
        --
        -- Every open touch of the CONTACT, not only the ones of the funnel
        -- chasing this order: the person just paid, and chasing them about a
        -- second abandoned cart in the same minute is the harm RF-033 and
        -- RF-034 exist to prevent.
        update public.scheduled_touches
           set status = 'cancelled',
               cancel_reason = 'stale_order_paid'
         where tenant_id = v_event.tenant_id
           and contact_id = v_contact_id
           and status in ('pending', 'enqueued');

        -- D8. Last touch before the money, inside the tenant's window. The
        -- window is READ, never written as a literal: `24` lives in
        -- `tenants.attribution_window_hours` and nowhere else.
        select attribution_window_hours
          into v_window
          from public.tenants
         where id = v_event.tenant_id;

        select t.id, t.funnel_id
          into v_touch_id, v_funnel_id
          from public.scheduled_touches t
         where t.tenant_id = v_event.tenant_id
           and t.contact_id = v_contact_id
           and t.status = 'sent'
           and t.sent_at <= v_paid_at
           and t.sent_at > v_paid_at - make_interval(hours => v_window)
         order by t.sent_at desc
         limit 1;

        if v_touch_id is not null then
            -- The value is COPIED out of the mirror, not joined to it later:
            -- the order can be re-synced, the contact purged and the messages
            -- aged out, and this row still says what was recovered and when.
            -- `on conflict do nothing` on the UNIQUE `order_id`: one payment
            -- credits one funnel, whatever redelivers.
            insert into public.funnel_conversions
                (tenant_id, funnel_id, contact_id, scheduled_touch_id, order_id,
                 amount, currency, attributed_at)
            select v_event.tenant_id, v_funnel_id, v_contact_id, v_touch_id, o.id,
                   coalesce(o.total, 0), o.currency, v_paid_at
              from public.orders o
             where o.id = v_order_id
            on conflict (order_id) do nothing;
        end if;
    end if;

    update internal.webhook_events
       set status = 'processed', processed_at = now()
     where id = p_webhook_event_id;

    -- `applied` with no conversation and no outbox row: a payment starts no
    -- conversation and sends nothing. What it applied is the mirror, and the
    -- silence it bought the contact.
    return row('applied', null, null)::internal.domain_event_outcome;
end
$$;

comment on function internal.apply_order_paid(bigint) is
    'D4/D7/D8: espelha o pedido, cancela na hora os toques abertos do contato '
    '(cancel_reason = stale_order_paid, o vocabulário da escada) e credita a conversão '
    'se houve toque enviado dentro de tenants.attribution_window_hours.';

-- ---------------------------------------------------------------------------
-- internal.apply_domain_event — o roteamento, e o alvo da guarda do CAS
-- ---------------------------------------------------------------------------
-- Identical to migration 20260806000003 except for two things:
--
--   * `order_paid` is no longer an unsupported type discarded with a trail — it
--     has a handler, and it needs neither a channel nor a funnel to do its work;
--
--   * an abandonment that CARRIES an order mirrors it and hands its id to
--     `start_funnel_run`. That parameter existed since S3 and nothing ever
--     passed it, so `internal.dispatch_touch`'s `order_unpaid` conjunct was
--     revalidating against NULL — a guard aimed at nothing. This is what gives
--     it a target, and it is what makes the race safe in the one order the
--     immediate cancellation of D7 cannot cover: the payment that lands BEFORE
--     the funnel exists. There the mirror is already `paid` when the cadence is
--     born, the ladder reads it in the snapshot and the first touch dies with
--     `stale_order_paid` instead of going out.
create or replace function internal.apply_domain_event(p_webhook_event_id bigint)
    returns internal.domain_event_outcome
    language plpgsql
    security definer
    set search_path = pg_catalog, public, internal
as $$
declare
    v_event      internal.webhook_events%rowtype;
    v_phone      text;
    v_channel_id uuid;
    v_order      jsonb;
    v_connector  uuid;
    v_contact_id uuid;
    v_order_id   uuid;
    v_run        internal.funnel_run_outcome;
begin
    select * into v_event
      from internal.webhook_events
     where id = p_webhook_event_id;

    if not found then
        raise exception 'domain event % does not exist', p_webhook_event_id;
    end if;

    -- Redelivery guard, first as always. pgmq redelivers for every normal
    -- reason; the second application must be a non-event — one abandoned cart,
    -- one funnel; one payment, one cancellation, one conversion.
    if v_event.status = 'processed' then
        return row('already_applied', null, null)::internal.domain_event_outcome;
    end if;

    -- The payment has its own handler since S5.
    if v_event.event_type = 'order_paid' then
        return internal.apply_order_paid(p_webhook_event_id);
    end if;

    -- Only the occasions a funnel can have. Anything else is discarded with a
    -- trail, never failed — reprocessing an unsupported type would not change
    -- the outcome.
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

    -- The order this funnel is chasing, when the event carries one. Absent — as
    -- it is for every platform that does not send the order with the
    -- abandonment yet — nothing is mirrored and the cadence is exactly what S3
    -- produced. Additive, both in the schema and in the behaviour.
    --
    -- Mirrored BEFORE the funnel lookup, and deliberately even when the outcome
    -- turns out to be `no_funnel`: an order is the merchant's own fact and the
    -- mirror is answerable to RF-070, not to whether a funnel was configured.
    -- That is a different thing from the person and the empty conversation
    -- `start_funnel_run` refuses to leave behind.
    v_order := v_event.payload -> 'order';
    if v_order is not null
       and jsonb_typeof(v_order) = 'object'
       and coalesce(v_order ->> 'external_id', '') <> ''
    then
        select id
          into v_connector
          from public.connector_accounts
         where tenant_id = v_event.tenant_id
           and platform = v_event.source
           and source_account_id = v_event.source_account_id;

        if v_connector is not null then
            select id
              into v_contact_id
              from public.contacts
             where tenant_id = v_event.tenant_id
               and phone_e164 = v_phone;

            -- NULL status: an abandonment makes no claim about payment, and
            -- saying `pending` here would push a paid order backwards.
            v_order_id := internal.mirror_order(
                v_event.tenant_id, v_connector, v_order, v_contact_id,
                null, v_event.received_at);
        end if;
    end if;

    -- `received_at`, never `now()`: the instant the platform's fact reached us
    -- is what staleness compares a newer message against.
    select * into v_run
      from internal.start_funnel_run(
               v_event.tenant_id,
               v_event.event_type,
               v_phone,
               v_channel_id,
               v_event.received_at,
               v_order_id);

    if v_run.status = 'no_funnel' then
        update internal.webhook_events
           set status = 'discarded', processed_at = now()
         where id = p_webhook_event_id;
        return row('no_funnel', null, null)::internal.domain_event_outcome;
    end if;

    -- The contact exists now, whether or not it did when the order was
    -- mirrored: `start_funnel_run` upserts it. This closes the link in the one
    -- order the mirror could not — first abandonment of a contact we had never
    -- seen — and it never overwrites one, so a re-synced order keeps its person.
    if v_order_id is not null then
        update public.orders o
           set contact_id = c.id
          from public.contacts c
         where o.id = v_order_id
           and c.tenant_id = v_event.tenant_id
           and c.phone_e164 = v_phone
           and o.contact_id is null;
    end if;

    update internal.webhook_events
       set status = 'processed', processed_at = now()
     where id = p_webhook_event_id;

    return row('applied', v_run.conversation_id, null)::internal.domain_event_outcome;
end
$$;

comment on type internal.domain_event_outcome is
    'status: applied | already_applied | discarded | invalid_payload | no_channel | no_funnel | '
    'no_store. O worker arquiva em todos — desfecho é dado, não exceção. Desde o E3 S3 `applied` '
    'significa "a cadência do funil existe"; desde o S5, para um `order_paid`, significa "o espelho '
    'foi escrito, os toques abertos morreram e a conversão foi creditada se havia uma". '
    'outbox_id é sempre nulo (D11).';

-- ---------------------------------------------------------------------------
-- Quem executa
-- ---------------------------------------------------------------------------
-- SECURITY DEFINER writing across tenants: minimum EXECUTE (ADR-11). The worker
-- is the only consumer of `q_domain_events`; ingestion produces, the sender
-- speaks, and neither has any business mirroring somebody's order book.
revoke execute on function internal.apply_order_paid(bigint) from public;
grant execute on function internal.apply_order_paid(bigint) to worker_role;

revoke execute on function
    internal.mirror_order(uuid, uuid, jsonb, uuid, text, timestamptz) from public;
grant execute on function
    internal.mirror_order(uuid, uuid, jsonb, uuid, text, timestamptz) to worker_role;

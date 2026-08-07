-- E3 · S5 (emenda) — uma conversão de valor desconhecido não vale zero.
--
-- `funnel_conversions.amount` nasceu `NOT NULL` no S2, e a consequência só
-- apareceu quando o handler do pagamento foi escrito: um pedido cujo payload
-- não traz `total` obrigava um `coalesce(total, 0)`, e isso transforma
-- "recuperei uma venda de valor desconhecido" em "recuperei R$ 0,00". As duas
-- frases são diferentes e viram a mesma dentro de um `sum()` — número
-- confiantemente errado, que é o pior tipo, e justamente sobre a métrica pela
-- qual o lojista julga o produto.
--
-- NULL preserva a distinção: a soma ignora o que não sabe, a contagem de
-- conversões continua contando o fato, e um relatório pode dizer "3 vendas
-- recuperadas, 1 sem valor informado" em vez de mentir uma média.
--
-- Expand-contract, e é o lado seguro do afrouxamento: o runtime N-1 sempre
-- manda um valor e segue funcionando contra este esquema. A contração (voltar a
-- NOT NULL) não existe e não deve existir.
--
-- O CHECK `amount >= 0` fica: uma comparação com NULL é `unknown`, e um CHECK
-- só rejeita o que é `false`. Valor desconhecido passa; valor negativo continua
-- barrado.
alter table public.funnel_conversions
    alter column amount drop not null;

comment on column public.funnel_conversions.amount is
    'Valor recuperado, COPIADO do espelho (o pedido pode sumir e o valor não pode). '
    'NULL tem significado declarado: "recuperado, valor desconhecido" — a plataforma '
    'não informou o total. Nunca 0, que é uma venda de zero real e uma frase diferente.';

-- ---------------------------------------------------------------------------
-- internal.apply_order_paid — sem o zero fabricado
-- ---------------------------------------------------------------------------
-- Idêntica à migration 20260806000005 exceto por uma expressão: `coalesce(o.total, 0)`
-- vira `o.total`. O corpo inteiro reaparece porque é assim que o PostgreSQL
-- troca o corpo de uma função — a mesma razão pela qual `apply_domain_event`
-- reapareceu inteira no S3 e no S5.
create or replace function internal.apply_order_paid(p_webhook_event_id bigint)
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
    -- must not count a touch that went out after it.
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
            -- `o.total` and NOT `coalesce(o.total, 0)`: the platform that sent
            -- no total leaves the amount unknown, and unknown is a value the
            -- column now carries. A fabricated zero would be a recovered sale
            -- worth nothing, which is a different sentence and an unrecoverable
            -- one once it is inside a `sum()`.
            --
            -- The value is COPIED out of the mirror, not joined to it later:
            -- the order can be re-synced, the contact purged and the messages
            -- aged out, and this row still says what was recovered and when.
            -- `on conflict do nothing` on the UNIQUE `order_id`: one payment
            -- credits one funnel, whatever redelivers.
            insert into public.funnel_conversions
                (tenant_id, funnel_id, contact_id, scheduled_touch_id, order_id,
                 amount, currency, attributed_at)
            select v_event.tenant_id, v_funnel_id, v_contact_id, v_touch_id, o.id,
                   o.total, o.currency, v_paid_at
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

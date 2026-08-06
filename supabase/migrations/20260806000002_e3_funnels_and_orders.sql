-- E3 · S2 — the schema slice of the milestone: the funnel, the touches it
-- schedules, the protections that can stop one, the order mirror the payment
-- arrives in, and the revenue that has to outlive both.
--
-- Sources: core/dicionario-de-dados.md §1.4, §3.1, §3.3, §3.4, §4.1, §4.4,
-- §5.4, §5.5 and the integrity notes of §8. Conventions from the same document
-- and from migrations 0001/0003: uuid PKs, timestamptz, enums as text + CHECK,
-- tenant_id + RLS on every business table, policies in the same migration as
-- the table.
--
-- Four decisions of the milestone are materialised here and nowhere else:
--
--   D1 — the platform ceiling of RF-034 is a CHECK, not a rule some service
--   remembers. `tenants.proactive_max_per_contact_24h` is bounded at 4 by the
--   database, written only by `internal.set_proactive_cap`, and guarded by a
--   trigger so that a future blanket GRANT cannot hand it to a merchant. Same
--   principle as Judge 1 being platform-fixed: a safety lock does not weaken by
--   customer configuration.
--
--   D2 — the ladder decides in Python and the write revalidates. Every field of
--   `dispatch.ladder.Guards` needs a column it can point at, or S4 would be
--   guessing: `event_at` (what staleness is measured against), `order_id` (the
--   order this funnel chases), `sent_at` (when a touch actually went out) and
--   `outbox_id` (which send it became) exist for that and nothing else.
--
--   D8 — recovered revenue is a recorded fact. `funnel_conversions` exists
--   because `messages` has a rolling TTL of 12 months and a conversation can be
--   deleted; a query that reconstructs attribution afterwards cannot be written.
--
--   The ladder's vocabulary IS the cancellation vocabulary. The dictionary's
--   four values (`replied | paid | suppressed | manual`) could not hold the nine
--   reasons `dispatch/ladder.py` produces, and flattening them would erase the
--   metric S11 asks for — cancelled touches BY REASON. `agents_runtime.dispatch
--   .ladder.DENIAL_REASONS` is the source; `manual` is added because an operator
--   cancelling by hand is a cancellation the ladder never produces.

-- ---------------------------------------------------------------------------
-- 1.4 audit_log — append-only
-- ---------------------------------------------------------------------------
-- Foreseen by the dictionary since E0 and only now needed: D1 requires the
-- ceiling change to be auditable, RNF-044 requires consent and its opposite to
-- be on the record, and S7 will write `evolution.risk_accepted` here.
--
-- Append-only is enforced by privilege, not by hope: nobody is granted UPDATE
-- or DELETE on this table, so an entry can be read but never edited away.
create table public.audit_log (
    id            bigint generated always as identity primary key,
    -- NULL = a platform action belonging to no tenant. The policies scope on
    -- equality and `tenant_id = null` is never true, so those rows are invisible
    -- to every tenant — the shape `alerts` already has.
    tenant_id     uuid        references public.tenants (id) on delete cascade,
    actor_type    text        not null check (actor_type in ('user', 'system', 'flywheel')),
    -- ON DELETE SET NULL: the trail outlives the account. Deleting a user must
    -- not delete the record of what they approved.
    actor_user_id uuid        references auth.users (id) on delete set null,
    action        text        not null check (length(action) > 0),
    target_type   text,
    target_id     uuid,
    payload       jsonb       not null default '{}'::jsonb,
    created_at    timestamptz not null default now()
);

create index audit_log_tenant_idx on public.audit_log (tenant_id, created_at desc);

comment on table public.audit_log is
    'Append-only trail of decisions a human is answerable for. No role is granted UPDATE or DELETE.';

-- ---------------------------------------------------------------------------
-- 3.4 customers — the mirror the prompt reads context from
-- ---------------------------------------------------------------------------
create table public.customers (
    id                   uuid primary key default gen_random_uuid(),
    tenant_id            uuid        not null references public.tenants (id) on delete cascade,
    connector_account_id uuid        not null references public.connector_accounts (id) on delete cascade,
    external_id          text        not null,
    name                 text,
    email                text,
    -- Same shape as `contacts.phone_e164`, because linking the two is the whole
    -- point: a phone stored in two shapes is two people who are one person.
    phone_e164           text        check (phone_e164 ~ '^\+[1-9][0-9]{7,14}$'),
    total_orders         integer     not null default 0 check (total_orders >= 0),
    total_spent          numeric(12, 2) not null default 0 check (total_spent >= 0),
    avg_ticket           numeric(12, 2) check (avg_ticket >= 0),
    first_order_at       timestamptz,
    last_order_at        timestamptz,
    synced_at            timestamptz not null default now(),
    created_at           timestamptz not null default now(),
    -- Per store, never global: platforms hand out per-store ids and "1" exists
    -- in every one of them.
    unique (connector_account_id, external_id)
);

create index customers_tenant_phone_idx on public.customers (tenant_id, phone_e164);

-- ---------------------------------------------------------------------------
-- 3.3 orders — the mirror the payment arrives in
-- ---------------------------------------------------------------------------
create table public.orders (
    id                   uuid primary key default gen_random_uuid(),
    tenant_id            uuid        not null references public.tenants (id) on delete cascade,
    connector_account_id uuid        not null references public.connector_accounts (id) on delete cascade,
    external_id          text        not null,
    -- Text, matching `customers.external_id`, and deliberately not an FK: the
    -- order event frequently arrives before the customer has been mirrored, and
    -- an FK would make the mirror depend on sync order.
    customer_external_id text,
    -- Fulfilment vocabulary, free text on purpose: it differs per platform,
    -- it is shown and never branched on.
    status               text,
    -- The opposite case, and the reason for the CHECK: integrity note 3 says
    -- `paid` is the signal that cancels a funnel. An unmapped platform word
    -- would not fail — it would simply never equal 'paid', and the merchant
    -- would keep chasing a contact who already paid. The connector adapter
    -- normalises the platform's vocabulary into this set; the CHECK is what
    -- makes forgetting to do so loud instead of silent.
    financial_status     text        not null default 'pending'
                             check (financial_status in ('pending', 'authorized', 'paid',
                                                         'partially_refunded', 'refunded',
                                                         'voided', 'cancelled')),
    total                numeric(12, 2),
    currency             text        not null default 'BRL' check (currency ~ '^[A-Z]{3}$'),
    items                jsonb       not null default '[]'::jsonb,
    tracking_code        text,
    tracking_status      text,
    platform_created_at  timestamptz,
    platform_updated_at  timestamptz,
    synced_at            timestamptz not null default now(),
    created_at           timestamptz not null default now(),
    unique (connector_account_id, external_id)
);

create index orders_tenant_customer_idx on public.orders (tenant_id, customer_external_id);

comment on column public.orders.financial_status is
    'Normalised by the connector adapter, never the platform''s raw word. '
    '`paid` is the signal that cancels a funnel (integrity note 3).';

-- ---------------------------------------------------------------------------
-- 4.1 contacts.customer_id — the omission of the E1 slice, closed
-- ---------------------------------------------------------------------------
-- Foreseen by the dictionary §4.1 and left out of E1 because `customers` was
-- not in that slice. ON DELETE SET NULL: re-syncing the mirror must never take
-- a contact — and its conversation — down with it.
alter table public.contacts
    add column customer_id uuid references public.customers (id) on delete set null;

-- ---------------------------------------------------------------------------
-- 4.4 suppression_list — the record of "do not message me"
-- ---------------------------------------------------------------------------
create table public.suppression_list (
    id         uuid primary key default gen_random_uuid(),
    tenant_id  uuid        not null references public.tenants (id) on delete cascade,
    contact_id uuid        not null references public.contacts (id) on delete cascade,
    -- The ladder maps these to its own reasons (`ladder._SUPPRESSION_REASONS`)
    -- and fails closed on anything it does not recognise.
    reason     text        not null
                   check (reason in ('explicit_block', 'no_response_after_3',
                                     'intent_optout', 'manual')),
    created_by text        not null check (created_by in ('system', 'agent', 'admin')),
    created_at timestamptz not null default now(),
    -- One row per contact: suppression is a state, not a log. A second reason
    -- for the same contact would make "is this contact suppressed?" a question
    -- with two answers.
    unique (tenant_id, contact_id)
);

comment on table public.suppression_list is
    'Checked before EVERY proactive send (RF-033). Opt-out suppresses sending, it does not erase data (RNF-044).';

-- ---------------------------------------------------------------------------
-- 5.4 funnels
-- ---------------------------------------------------------------------------
create table public.funnels (
    id                 uuid primary key default gen_random_uuid(),
    tenant_id          uuid        not null references public.tenants (id) on delete cascade,
    occasion           text        not null
                           check (occasion in ('pix_pending', 'checkout_abandoned',
                                               'cart_abandoned')),
    enabled            boolean     not null default true,
    channel_preference text        not null default 'auto'
                           check (channel_preference in ('cloud', 'evolution', 'auto')),
    -- `[{n, delay, template_ref/copy_base, cta}]` and nothing else (risk R5 of
    -- the plan): a conditional inside a funnel is a rule that wanted to be code.
    touches            jsonb       not null default '[]'::jsonb,
    -- The ceiling of THIS funnel's cadence, not a per-contact limit. The
    -- per-contact protections are the ladder's (RF-034) and live on `tenants`.
    max_touches        integer     not null default 4 check (max_touches > 0),
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now()
);

-- `start_funnel_run` asks "does this occasion have an enabled funnel?". Two
-- answers is not a configuration, it is a coin toss — and the same device as
-- `agent_versions_one_active_per_tenant`: partial, so disabled funnels pile up
-- freely as history.
create unique index funnels_one_enabled_per_occasion
    on public.funnels (tenant_id, occasion)
    where enabled;

create trigger funnels_touch_updated_at
    before update on public.funnels
    for each row execute function public.touch_updated_at();

-- ---------------------------------------------------------------------------
-- 5.5 scheduled_touches — every proactive touch, before it exists
-- ---------------------------------------------------------------------------
-- D11: `start_funnel_run` writes ONLY here, with touch nº 1 already due. The
-- dispatcher of S4 is the single door to the outbox, so the ladder cannot be
-- bypassed by a fast path.
create table public.scheduled_touches (
    id              uuid primary key default gen_random_uuid(),
    tenant_id       uuid        not null references public.tenants (id) on delete cascade,
    funnel_id       uuid        not null references public.funnels (id) on delete cascade,
    contact_id      uuid        not null references public.contacts (id) on delete cascade,
    -- Nullable, and SET NULL rather than CASCADE: a touch can precede the
    -- conversation it will start, and it must outlive one that was purged —
    -- otherwise the 72h cooldown would forget touches that did go out.
    conversation_id uuid        references public.conversations (id) on delete set null,
    -- The order this funnel is chasing. Without it the ladder's `order_unpaid`
    -- guard has nothing to revalidate against, and "payment cancels the funnel"
    -- becomes a lookup by guesswork.
    order_id        uuid        references public.orders (id) on delete set null,
    touch_number    integer     not null check (touch_number > 0),
    -- When the fact that justifies this touch happened — the abandonment, the
    -- unpaid PIX. NOT when the touch was scheduled, and deliberately WITHOUT a
    -- default: `now()` would quietly substitute the scheduling instant for the
    -- event instant and break exactly the late-event case staleness exists for.
    event_at        timestamptz not null,
    due_at          timestamptz not null,
    status          text        not null default 'pending'
                        check (status in ('pending', 'enqueued', 'sent', 'cancelled')),
    -- The ladder's nine reasons plus `manual`. Kept in sync with
    -- `agents_runtime.dispatch.ladder.DENIAL_REASONS` by a test that reads both.
    cancel_reason   text
                        check (cancel_reason in ('suppressed_block', 'suppressed_silence',
                                                 'suppressed_optout', 'quota_exceeded',
                                                 'stale_newer_message', 'stale_order_paid',
                                                 'rate_limit_24h', 'funnel_cooldown_72h',
                                                 'channel_paused_tier', 'manual')),
    -- The instant the touch was committed to the outbox, which is what the 72h
    -- cooldown between funnels measures. Delivery happens later, in the sender;
    -- counting the commitment rather than the delivery can only suppress a
    -- send, never create one.
    sent_at         timestamptz,
    -- Which send it became. ON DELETE SET NULL, never CASCADE — draining the
    -- outbox must not erase the fact that a contact was touched.
    outbox_id       uuid        references internal.message_outbox (id) on delete set null,
    created_at      timestamptz not null default now(),
    -- Half-set states, forbidden for the reason `conversations_lease_is_whole`
    -- gives: a `sent` touch with no instant is invisible to the cooldown, and a
    -- `cancelled` touch with no reason still moves the total while destroying
    -- the only number that diagnoses it (S11).
    constraint scheduled_touches_sent_is_whole
        check ((status = 'sent') = (sent_at is not null)),
    constraint scheduled_touches_cancel_is_whole
        check ((status = 'cancelled') = (cancel_reason is not null))
);

-- The dispatcher's scan, every minute (dictionary §5.5).
create index scheduled_touches_due_idx on public.scheduled_touches (status, due_at);

-- The 72h cooldown between DIFFERENT funnels. Funnel identity exists in no
-- other table, which is why this window is measured here and the 24h one is not.
create index scheduled_touches_contact_sent_idx
    on public.scheduled_touches (contact_id, sent_at desc)
    where status = 'sent';

-- What `order_paid` cancels (integrity note 3) and what an operator lists.
create index scheduled_touches_contact_open_idx
    on public.scheduled_touches (contact_id)
    where status in ('pending', 'enqueued');

comment on column public.scheduled_touches.sent_at is
    'Set when the outbox row is written, not when the API accepts. The 72h '
    'cooldown of RF-034 is measured from here.';

-- ---------------------------------------------------------------------------
-- funnel_conversions (D8) — new table, folded into the dictionary in this PR
-- ---------------------------------------------------------------------------
-- Attribution is a recorded fact, never a query. `messages` has a rolling TTL
-- of 12 months and a conversation can be deleted; the value and the currency
-- are copied here rather than joined for the same reason.
create table public.funnel_conversions (
    id                  uuid primary key default gen_random_uuid(),
    tenant_id           uuid        not null references public.tenants (id) on delete cascade,
    -- Everything below is SET NULL on delete: what happened must survive the
    -- disappearance of what it happened to. A per-contact purge severs the
    -- person and leaves the money, which is exactly what LGPD asks for.
    funnel_id           uuid        references public.funnels (id) on delete set null,
    contact_id          uuid        references public.contacts (id) on delete set null,
    scheduled_touch_id  uuid        references public.scheduled_touches (id) on delete set null,
    order_id            uuid        references public.orders (id) on delete set null,
    amount              numeric(12, 2) not null check (amount >= 0),
    currency            text        not null default 'BRL' check (currency ~ '^[A-Z]{3}$'),
    attributed_at       timestamptz not null default now(),
    created_at          timestamptz not null default now(),
    -- One payment credits one funnel. Two rows for the same order would double
    -- the only number the merchant bought the product for.
    unique (order_id)
);

create index funnel_conversions_tenant_idx
    on public.funnel_conversions (tenant_id, attributed_at desc);

comment on table public.funnel_conversions is
    'Recovered revenue as a fact (D8). Paid order within tenants.attribution_window_hours of a sent touch.';

-- ---------------------------------------------------------------------------
-- 3.1 channels_accounts — the anti-ban columns E1 recorded as this milestone's debt
-- ---------------------------------------------------------------------------
-- Migration 0003 said it out loud: "the anti-ban columns (meta_tier, warm-up,
-- daily cap, risk acceptance) and the Vault reference belong to E3 and T4; they
-- are additive and arrive with the code that reads them". They arrive here; the
-- sender reads them in S7.
alter table public.channels_accounts
    -- Cloud: the Meta conversation tier for this number. NULL = no tier, which
    -- is what Evolution is; the ladder reads NULL as "this rung does not apply".
    add column meta_tier         integer     check (meta_tier is null or meta_tier > 0),
    add column tier_usage_24h    integer     not null default 0 check (tier_usage_24h >= 0),
    -- Evolution: warm-up 20 → 50 → 100 and the hard daily cap of 300.
    add column warmup_stage      integer     not null default 0 check (warmup_stage >= 0),
    add column warmup_started_at timestamptz,
    add column daily_cap         integer     not null default 300 check (daily_cap > 0),
    -- The merchant accepting the ban risk of an unofficial channel (RNF-044).
    -- Mirrored into audit_log by the code that sets it, in S7.
    add column risk_accepted_at  timestamptz,
    -- Vault reference, read only through `get_channel_secret()` (RNF-032).
    add column vault_secret_id   uuid;

-- ---------------------------------------------------------------------------
-- 1.1 tenants — the two knobs of the milestone
-- ---------------------------------------------------------------------------
alter table public.tenants
    -- D1 / RF-034. Default 1, absolute platform ceiling 4, enforced by the
    -- database so that no write path anywhere can exceed it. Zero is not
    -- "tighter": it would switch the product off through a number nobody meant
    -- as a switch, and the merchant has `funnels.enabled` for that.
    add column proactive_max_per_contact_24h smallint not null default 1
        check (proactive_max_per_contact_24h between 1 and 4),
    -- D8. How long after a touch a paid order still counts as recovered.
    add column attribution_window_hours smallint not null default 24
        check (attribution_window_hours > 0);

comment on column public.tenants.proactive_max_per_contact_24h is
    'RF-034 / D1. Written ONLY by internal.set_proactive_cap(). The merchant may '
    'tighten; loosening towards the ceiling of 4 is an admin action.';

-- The lock, in two halves.
--
-- Half one is the privilege: no role but the owner holds UPDATE on
-- `public.tenants`, so no merchant credential can reach the column today.
--
-- Half two is this trigger, and it exists because half one is not durable. The
-- moment E5 grants the hub `UPDATE ON public.tenants` for the settings screen,
-- a column-level revoke written here would be silently re-granted and every
-- test asserting "permission denied" would keep passing for the wrong reason
-- (decisão 16). The trigger does not care about privileges: it refuses any
-- change to the column that did not come through the function.
--
-- Honest about its limit: `app.proactive_cap_change` is a session setting, so a
-- role that can run arbitrary SQL *and* holds UPDATE could set it and slip
-- through. That role does not exist in the product — the hub speaks to Postgres
-- only through PostgREST — and the guard is aimed at the accident, not at a
-- superuser who has already lost the argument.
create function public.guard_proactive_cap()
    returns trigger
    language plpgsql
    set search_path = pg_catalog
as $$
begin
    if new.proactive_max_per_contact_24h is distinct from old.proactive_max_per_contact_24h
       and coalesce(current_setting('app.proactive_cap_change', true), 'off') <> 'on'
    then
        raise exception
            'proactive_max_per_contact_24h is written only by internal.set_proactive_cap()';
    end if;

    return new;
end
$$;

create trigger tenants_guard_proactive_cap
    before update on public.tenants
    for each row execute function public.guard_proactive_cap();

-- The one write path (D1). SECURITY DEFINER with a fixed search_path and
-- EXECUTE revoked from PUBLIC, per ADR-11.
--
-- The ceiling of 4 is NOT restated here. One copy of a canonical number is a
-- rule; two copies are a future disagreement, so the function lets the CHECK
-- raise and the caller sees the constraint that actually governs.
--
-- Granted to nobody. `worker_role` processes hostile input from contacts and
-- `sender_role` drains a queue; neither has any business raising a rate limit,
-- and the Data API roles cannot even see the schema. The admin plane connects
-- with its own credential (E6), and the merchant's tightening reaches the same
-- function through the hub's server side — never through the browser's JWT.
create function internal.set_proactive_cap(
    p_tenant_id uuid,
    p_value     integer,
    p_actor     uuid default null
)
    returns void
    language plpgsql
    security definer
    set search_path = pg_catalog, public, internal
as $$
declare
    v_previous smallint;
begin
    select proactive_max_per_contact_24h
      into v_previous
      from public.tenants
     where id = p_tenant_id
       for update;

    if not found then
        raise exception 'tenant % does not exist', p_tenant_id;
    end if;

    perform set_config('app.proactive_cap_change', 'on', true);

    update public.tenants
       set proactive_max_per_contact_24h = p_value
     where id = p_tenant_id;

    perform set_config('app.proactive_cap_change', 'off', true);

    -- Same transaction as the update: a rejected value rolls the entry back
    -- with it. An audit log that records attempts as facts is worse than none.
    insert into public.audit_log
        (tenant_id, actor_type, actor_user_id, action, target_type, target_id, payload)
    values (p_tenant_id,
            case when p_actor is null then 'system' else 'user' end,
            p_actor,
            'tenant.proactive_cap_set',
            'tenant',
            p_tenant_id,
            jsonb_build_object('from', v_previous, 'to', p_value));
end
$$;

revoke execute on function internal.set_proactive_cap(uuid, integer, uuid) from public;

-- ---------------------------------------------------------------------------
-- The 24h window of RF-034 needs an index, and it is not on this milestone's tables
-- ---------------------------------------------------------------------------
-- The limit sums ALL origins — funnel touches, follow-ups, whatever campaign
-- means later — so the source is the outbox, the single door every proactive
-- send passes through, and never `scheduled_touches`, which only knows funnels.
-- Counted by `created_at` (the commitment) rather than `sent_at` (the delivery):
-- a touch already written and not yet delivered has to count, or a burst slips
-- two touches past a limit of one.
create index message_outbox_proactive_contact_idx
    on internal.message_outbox (contact_id, created_at desc)
    where kind in ('funnel_touch', 'followup');

-- ---------------------------------------------------------------------------
-- Grants — who may touch what
-- ---------------------------------------------------------------------------
-- Same reasoning as migrations 0001 and 0003: the roles get the privilege and
-- RLS does the scoping. Withholding the GRANT would also stop a cross-tenant
-- read, but it would prove nothing about the policy — "permission denied" is
-- not evidence of RLS (decisão 16).

-- The runtime: reads the funnel, works the touches, mirrors orders and
-- customers, writes suppressions and credits conversions.
grant select                         on public.funnels            to worker_role;
grant select, insert, update         on public.scheduled_touches  to worker_role;
grant select, insert, delete         on public.suppression_list   to worker_role;
grant select, insert, update         on public.orders             to worker_role;
grant select, insert, update         on public.customers          to worker_role;
grant select, insert                 on public.funnel_conversions to worker_role;
-- Append-only: SELECT and INSERT, never UPDATE or DELETE.
grant select, insert                 on public.audit_log          to worker_role;

-- `sender_role` gets nothing here. It drains the outbox; a funnel, a
-- suppression list and somebody's order book are none of its business. The
-- anti-ban counters it will bump in S7 are on `channels_accounts` and arrive
-- with the code that reads them.

-- The hub reads all of it through the Data API; RLS scopes it to the tenants
-- the user belongs to. Writes arrive with the screens that need them, in E5 —
-- an unused write grant is a leak waiting for a bug.
grant select on public.funnels, public.scheduled_touches, public.suppression_list,
               public.orders, public.customers, public.funnel_conversions,
               public.audit_log
          to authenticated;

-- ---------------------------------------------------------------------------
-- Row level security
-- ---------------------------------------------------------------------------
-- Policies ship in the same migration that creates the tables, for the reason
-- recorded in migration 0001: a table that exists for even one migration with a
-- GRANT and no policy was readable across tenants at some point in the schema's
-- history.
--
-- The red→green cycle that produced this still happened — the leak suite ran
-- against these seven tables with the GRANTs above and no policies, and watched
-- rows of the wrong tenant come back through the JWT and through `worker_role`.
-- That evidence lives in the commit message, not in a permanently insecure
-- migration.

alter table public.funnels            enable row level security;
alter table public.scheduled_touches  enable row level security;
alter table public.suppression_list   enable row level security;
alter table public.orders             enable row level security;
alter table public.customers          enable row level security;
alter table public.funnel_conversions enable row level security;
alter table public.audit_log          enable row level security;

-- funnels — configuration the runtime reads and the merchant edits in E5.
create policy funnels_member_read on public.funnels
    for select to authenticated
    using (tenant_id in (select public.user_tenant_ids()));

create policy funnels_worker_read on public.funnels
    for select to worker_role
    using (tenant_id = public.current_app_tenant_id());

-- scheduled_touches — USING scopes what may be seen and changed, WITH CHECK
-- what may be written: without the second half a worker scoped to A could
-- schedule a touch into B's funnel.
create policy scheduled_touches_member_read on public.scheduled_touches
    for select to authenticated
    using (tenant_id in (select public.user_tenant_ids()));

create policy scheduled_touches_worker_scoped on public.scheduled_touches
    for all to worker_role
    using (tenant_id = public.current_app_tenant_id())
    with check (tenant_id = public.current_app_tenant_id());

-- suppression_list — the nastiest row in the milestone to lose: deleting
-- somebody else's "do not message me" is silent, and the next touch goes out.
create policy suppression_list_member_read on public.suppression_list
    for select to authenticated
    using (tenant_id in (select public.user_tenant_ids()));

create policy suppression_list_worker_scoped on public.suppression_list
    for all to worker_role
    using (tenant_id = public.current_app_tenant_id())
    with check (tenant_id = public.current_app_tenant_id());

-- orders / customers — the mirror of somebody else's business.
create policy orders_member_read on public.orders
    for select to authenticated
    using (tenant_id in (select public.user_tenant_ids()));

create policy orders_worker_scoped on public.orders
    for all to worker_role
    using (tenant_id = public.current_app_tenant_id())
    with check (tenant_id = public.current_app_tenant_id());

create policy customers_member_read on public.customers
    for select to authenticated
    using (tenant_id in (select public.user_tenant_ids()));

create policy customers_worker_scoped on public.customers
    for all to worker_role
    using (tenant_id = public.current_app_tenant_id())
    with check (tenant_id = public.current_app_tenant_id());

-- funnel_conversions
create policy funnel_conversions_member_read on public.funnel_conversions
    for select to authenticated
    using (tenant_id in (select public.user_tenant_ids()));

create policy funnel_conversions_worker_scoped on public.funnel_conversions
    for all to worker_role
    using (tenant_id = public.current_app_tenant_id())
    with check (tenant_id = public.current_app_tenant_id());

-- audit_log — platform rows (`tenant_id is null`) are invisible to every
-- tenant, because equality against NULL is never true.
create policy audit_log_member_read on public.audit_log
    for select to authenticated
    using (tenant_id in (select public.user_tenant_ids()));

create policy audit_log_worker_scoped on public.audit_log
    for all to worker_role
    using (tenant_id = public.current_app_tenant_id())
    with check (tenant_id = public.current_app_tenant_id());

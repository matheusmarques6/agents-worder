-- E1 · PR-0 — the slice of the schema the steel thread needs.
--
-- Seven tables and nothing else. Everything here is demanded by the flow a
-- message takes from the webhook to the WhatsApp reply; `orders`, `customers`,
-- `products`, `agent_versions`, funnels, scheduled touches, knowledge and the
-- eval tables are deliberately absent, because each one costs policies and a
-- leak suite and none of them is on the wire.
--
-- Source: core/dicionario-de-dados.md §3.1, §3.2, §4.1–4.3, §5.1, §5.3.
-- Conventions and the RLS shape come from migration 0001.

-- ---------------------------------------------------------------------------
-- The internal schema (ADR-11)
-- ---------------------------------------------------------------------------
-- "Tabelas internas (outbox, filas, evals) fora do schema exposto pela Data
-- API". `public` IS exposed — supabase/config.toml lists it — so an internal
-- table living there is one GRANT away from being served over HTTP. A separate
-- schema makes that impossible by construction rather than by vigilance, the
-- same way `pgmq` is already out of reach.
create schema if not exists internal;

comment on schema internal is
    'Tables the platform runs on and nobody browses: webhook events and the send outbox. Never added to the exposed schemas of config.toml.';

-- Belt and braces: a schema created here grants nothing to PUBLIC, but saying
-- so out loud is what a reader checks.
revoke all on schema internal from public;
grant usage on schema internal to worker_role, sender_role;

-- ---------------------------------------------------------------------------
-- 3.2 connector_accounts — the store on the e-commerce platform
-- ---------------------------------------------------------------------------
-- `source_account_id` is the key that resolves the tenant during ingestion.
-- That is the whole reason this table is in the slice: without it,
-- `ingest_webhook` would have to believe a `tenant_id` in the payload, and the
-- trust boundary says it never does.
create table public.connector_accounts (
    id                  uuid primary key default gen_random_uuid(),
    tenant_id           uuid        not null references public.tenants (id) on delete cascade,
    platform            text        not null
                            check (platform in ('shopify', 'nuvemshop', 'yampi')),
    source_account_id   text        not null,
    sync_status         text        not null default 'ok'
                            check (sync_status in ('ok', 'syncing', 'error')),
    last_sync_at        timestamptz,
    webhooks_registered boolean     not null default false,
    created_at          timestamptz not null default now(),
    -- Global, not per tenant: the same store cannot belong to two tenants, and
    -- this is the constraint that says so.
    unique (platform, source_account_id)
);

-- ---------------------------------------------------------------------------
-- 3.1 channels_accounts — the WhatsApp number
-- ---------------------------------------------------------------------------
-- `external_account_id` is NOT in the data dictionary and is added here on
-- purpose: it is the channel's equivalent of `source_account_id`, the id the
-- provider puts in its webhook (Cloud API `phone_number_id`, Evolution
-- instance). Without it there is no way to resolve the tenant of an inbound
-- message except by trusting the payload — which the trust boundary forbids.
-- Recorded as a divergence to fold back into core/dicionario-de-dados.md §3.1.
--
-- The anti-ban columns (meta_tier, warm-up, daily cap, risk acceptance) and the
-- Vault reference belong to E3 and T4; they are additive and arrive with the
-- code that reads them.
create table public.channels_accounts (
    id                  uuid primary key default gen_random_uuid(),
    tenant_id           uuid        not null references public.tenants (id) on delete cascade,
    type                text        not null check (type in ('cloud', 'evolution')),
    phone_e164          text        not null check (phone_e164 ~ '^\+[1-9][0-9]{7,14}$'),
    external_account_id text        not null,
    display_name        text,
    status              text        not null default 'connecting'
                            check (status in ('connecting', 'active', 'paused', 'banned', 'error')),
    created_at          timestamptz not null default now(),
    unique (type, phone_e164),
    unique (type, external_account_id)
);

-- ---------------------------------------------------------------------------
-- 4.1 contacts
-- ---------------------------------------------------------------------------
-- `customer_id` is absent because `customers` is not in this slice. E.164 is
-- enforced by CHECK rather than by convention: a phone stored in two shapes is
-- two contacts for the same person, and the dedup never happens later.
create table public.contacts (
    id              uuid primary key default gen_random_uuid(),
    tenant_id       uuid        not null references public.tenants (id) on delete cascade,
    phone_e164      text        not null check (phone_e164 ~ '^\+[1-9][0-9]{7,14}$'),
    name            text,
    language        text,
    opt_status      text        not null default 'pending'
                        check (opt_status in ('pending', 'authorized', 'blocked')),
    first_seen_at   timestamptz not null default now(),
    last_message_at timestamptz,
    created_at      timestamptz not null default now(),
    unique (tenant_id, phone_e164)
);

-- ---------------------------------------------------------------------------
-- 4.2 conversations — where the counters, the lease and the generation live
-- ---------------------------------------------------------------------------
-- This is the table the whole engine turns on. Four groups of columns:
--
--   · the counters (`next_inbound_seq`, `next_outbound_seq`) that make
--     `SELECT max(seq)+1` unnecessary and therefore forbidden (ADR-6b);
--   · the debounce (`pending_response_at`) and the generation the coalescer
--     bumps, which together guarantee one job per burst (ADR-7);
--   · the lease (`processing_token`, `processing_until`) and `version`, which
--     are the compare-and-set that replaces a transactional advisory lock
--     (ADR-6);
--   · `last_processed_seq`, the answer to "did we already reply to this?".
create table public.conversations (
    id                    uuid primary key default gen_random_uuid(),
    tenant_id             uuid        not null references public.tenants (id) on delete cascade,
    contact_id            uuid        not null references public.contacts (id) on delete cascade,
    channel_account_id    uuid        not null references public.channels_accounts (id),
    state                 text        not null default 'ia'
                              check (state in ('ia', 'humano', 'encerrada')),
    origin_occasion       text        not null
                              check (origin_occasion in ('pix_pending', 'checkout_abandoned',
                                                         'cart_abandoned', 'direct', 'campaign')),
    slots                 jsonb       not null default '{}'::jsonb,
    -- Debounce deadline. A new message pushes it; the coalescer clears it.
    pending_response_at   timestamptz,
    last_processed_seq    integer     not null default 0,
    next_inbound_seq      integer     not null default 0,
    next_outbound_seq     integer     not null default 0,
    -- Bumped ONLY by the coalescer, inside its single transaction.
    processing_generation integer     not null default 0,
    processing_token      uuid,
    processing_until      timestamptz,
    version               integer     not null default 0,
    takeover_user_id      uuid        references auth.users (id),
    takeover_at           timestamptz,
    closed_at             timestamptz,
    created_at            timestamptz not null default now(),
    updated_at            timestamptz not null default now(),
    -- A half-set lease is a bug in whoever wrote it, and it would make the
    -- claim condition (`processing_until < now()`) read as "free" while a token
    -- still points at an owner.
    constraint conversations_lease_is_whole
        check ((processing_token is null) = (processing_until is null))
);

create index conversations_tenant_contact_idx on public.conversations (tenant_id, contact_id);

-- The coalescer's scan, every 2 seconds. Partial because the overwhelming
-- majority of conversations are not waiting on a deadline.
create index conversations_pending_idx on public.conversations (pending_response_at)
    where pending_response_at is not null;

create trigger conversations_touch_updated_at
    before update on public.conversations
    for each row execute function public.touch_updated_at();

-- ---------------------------------------------------------------------------
-- 5.1 webhook_events [interna] — idempotency of ingestion
-- ---------------------------------------------------------------------------
-- The UNIQUE includes `source_account_id` for a reason worth stating: platforms
-- hand out per-store sequential ids, so store A and store B both have an event
-- "1042". Keyed on (source, external_event_id) alone, the second store's event
-- would be silently swallowed as a duplicate of the first store's.
create table internal.webhook_events (
    id                bigint generated always as identity primary key,
    source            text        not null
                          check (source in ('shopify', 'nuvemshop', 'yampi', 'meta', 'evolution')),
    source_account_id text        not null,
    external_event_id text        not null,
    tenant_id         uuid        not null references public.tenants (id) on delete cascade,
    event_type        text        not null,
    payload           jsonb       not null,
    status            text        not null default 'received'
                          check (status in ('received', 'enqueued', 'processed',
                                            'discarded', 'failed')),
    received_at       timestamptz not null default now(),
    processed_at      timestamptz,
    unique (source, source_account_id, external_event_id)
);

-- ---------------------------------------------------------------------------
-- 5.3 message_outbox [interna] — nothing reaches WhatsApp except through here
-- ---------------------------------------------------------------------------
-- Written by the worker inside the FASE 3 transaction, drained by the sender
-- under its own lease. That split is why the two roles get different grants on
-- the same table: `agent_core` may create a send, only `sender_role` may
-- perform one.
create table internal.message_outbox (
    id                  uuid primary key default gen_random_uuid(),
    tenant_id           uuid        not null references public.tenants (id) on delete cascade,
    -- Nullable: a funnel touch can precede the conversation it will start.
    conversation_id     uuid        references public.conversations (id) on delete cascade,
    contact_id          uuid        not null references public.contacts (id) on delete cascade,
    channel_account_id  uuid        not null references public.channels_accounts (id),
    kind                text        not null
                            check (kind in ('reply', 'funnel_touch', 'followup', 'correction')),
    payload             jsonb       not null,
    -- Deterministic, and it travels in `biz_opaque_callback_data` so a status
    -- webhook can be correlated back to this row (ADR-8).
    idempotency_key     text        not null unique,
    payload_hash        text,
    status              text        not null default 'pending'
                            check (status in ('pending', 'sending', 'sent', 'failed',
                                              'unknown', 'manual_review')),
    attempt_count       integer     not null default 0,
    next_attempt_at     timestamptz not null default now(),
    locked_by           text,
    locked_until        timestamptz,
    request_started_at  timestamptz,
    last_error          text,
    provider_message_id text,
    created_at          timestamptz not null default now(),
    sent_at             timestamptz
);

create index message_outbox_claim_idx on internal.message_outbox (status, next_attempt_at);

-- ---------------------------------------------------------------------------
-- 4.3 messages — what was said, in order
-- ---------------------------------------------------------------------------
create table public.messages (
    id                  uuid primary key default gen_random_uuid(),
    tenant_id           uuid        not null references public.tenants (id) on delete cascade,
    conversation_id     uuid        not null references public.conversations (id) on delete cascade,
    direction           text        not null check (direction in ('inbound', 'outbound')),
    seq                 integer     not null check (seq > 0),
    channel             text        not null
                            check (channel in ('whatsapp_cloud', 'whatsapp_evolution')),
    author_type         text        not null check (author_type in ('contact', 'agent', 'human')),
    author_user_id      uuid        references auth.users (id),
    content             jsonb       not null,
    provider_message_id text,
    outbox_id           uuid        references internal.message_outbox (id) on delete set null,
    -- Rolling TTL, filled by the trigger below from the tenant's retention.
    expires_at          timestamptz not null,
    created_at          timestamptz not null default now(),
    -- The constraint that makes a duplicated sequence impossible rather than
    -- merely unlikely (ADR-6b).
    unique (conversation_id, direction, seq)
);

create index messages_expiry_idx on public.messages (expires_at);

-- `expires_at` is NOT NULL and nobody writes it by hand. A write path that
-- forgot it would either fail loudly or, worse, be "fixed" by making the column
-- nullable — and a message with no expiry is a message the LGPD purge misses.
create function public.set_message_expiry()
    returns trigger
    language plpgsql
    set search_path = pg_catalog, public
as $$
begin
    if new.expires_at is null then
        select now() + make_interval(months => t.retention_months)
          into new.expires_at
          from public.tenants t
         where t.id = new.tenant_id;
    end if;
    return new;
end
$$;

create trigger messages_set_expiry
    before insert on public.messages
    for each row execute function public.set_message_expiry();

-- ---------------------------------------------------------------------------
-- Grants — who may touch what
-- ---------------------------------------------------------------------------
-- Same reasoning as migration 0001: the roles get the privilege and RLS does
-- the scoping. Withholding the GRANT would also stop a cross-tenant read, but
-- it would prove nothing about the policy — and "permission denied" is not
-- evidence of RLS (decisão 16).

-- The worker runs the conversation.
grant select                         on public.connector_accounts to worker_role;
grant select                         on public.channels_accounts  to worker_role;
grant select, insert, update         on public.contacts           to worker_role;
grant select, insert, update         on public.conversations      to worker_role;
grant select, insert                 on public.messages           to worker_role;
grant select, insert, update         on internal.webhook_events   to worker_role;
-- Creates a send; never performs one.
grant select, insert                 on internal.message_outbox   to worker_role;

-- The sender performs it.
grant select                         on public.channels_accounts  to sender_role;
grant select, update                 on internal.message_outbox   to sender_role;

-- The hub reads the conversation through the Data API as `authenticated`; RLS
-- scopes it to the tenants the user belongs to. Writes arrive with the screens
-- that need them, in E5.
grant select on public.contacts, public.conversations, public.messages to authenticated;

-- Nothing internal is ever granted to a Data API role. Stated rather than
-- assumed, and asserted by tests/db/test_internal_schema.py.
revoke all on all tables in schema internal from anon, authenticated, service_role;
revoke all on schema internal from anon, authenticated, service_role;

-- ---------------------------------------------------------------------------
-- Row level security
-- ---------------------------------------------------------------------------
-- Policies ship in the same migration as the tables, for the reason recorded in
-- migration 0001: a table that exists for even one migration with a GRANT and
-- no policy was readable across tenants at some point in the schema's history.

alter table public.connector_accounts enable row level security;
alter table public.channels_accounts  enable row level security;
alter table public.contacts           enable row level security;
alter table public.conversations      enable row level security;
alter table public.messages           enable row level security;
alter table internal.webhook_events   enable row level security;
alter table internal.message_outbox   enable row level security;

-- connector_accounts / channels_accounts — configuration the runtime reads.
create policy connector_accounts_worker_read on public.connector_accounts
    for select to worker_role
    using (tenant_id = public.current_app_tenant_id());

create policy channels_accounts_worker_read on public.channels_accounts
    for select to worker_role
    using (tenant_id = public.current_app_tenant_id());

create policy channels_accounts_sender_read on public.channels_accounts
    for select to sender_role
    using (tenant_id = public.current_app_tenant_id());

-- contacts
create policy contacts_member_read on public.contacts
    for select to authenticated
    using (tenant_id in (select public.user_tenant_ids()));

create policy contacts_worker_scoped on public.contacts
    for all to worker_role
    using (tenant_id = public.current_app_tenant_id())
    with check (tenant_id = public.current_app_tenant_id());

-- conversations
create policy conversations_member_read on public.conversations
    for select to authenticated
    using (tenant_id in (select public.user_tenant_ids()));

create policy conversations_worker_scoped on public.conversations
    for all to worker_role
    using (tenant_id = public.current_app_tenant_id())
    with check (tenant_id = public.current_app_tenant_id());

-- messages
create policy messages_member_read on public.messages
    for select to authenticated
    using (tenant_id in (select public.user_tenant_ids()));

create policy messages_worker_scoped on public.messages
    for all to worker_role
    using (tenant_id = public.current_app_tenant_id())
    with check (tenant_id = public.current_app_tenant_id());

-- webhook_events — internal, worker only.
create policy webhook_events_worker_scoped on internal.webhook_events
    for all to worker_role
    using (tenant_id = public.current_app_tenant_id())
    with check (tenant_id = public.current_app_tenant_id());

-- message_outbox — written by the worker, performed by the sender. Two roles,
-- two policies, one table.
create policy message_outbox_worker_write on internal.message_outbox
    for all to worker_role
    using (tenant_id = public.current_app_tenant_id())
    with check (tenant_id = public.current_app_tenant_id());

create policy message_outbox_sender_scoped on internal.message_outbox
    for all to sender_role
    using (tenant_id = public.current_app_tenant_id())
    with check (tenant_id = public.current_app_tenant_id());

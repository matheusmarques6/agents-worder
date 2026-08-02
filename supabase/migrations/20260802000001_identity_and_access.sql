-- E0-07 · Identity and access — the first three tables and the two app roles.
--
-- Scope is deliberately the minimum that gives the `rls` suite something real
-- to assert against: `memberships` is the first table carrying `tenant_id`,
-- and therefore the first genuine cross-tenant leak target in the system.
--
-- Source: core/dicionario-de-dados.md §1.1–1.3. Conventions from the same
-- document: uuid PKs, timestamptz everywhere, enums as text + CHECK so they
-- evolve without ALTER TYPE, tenant_id + RLS on every business table.

-- ---------------------------------------------------------------------------
-- 1.1 tenants
-- ---------------------------------------------------------------------------
create table public.tenants (
    id                uuid primary key default gen_random_uuid(),
    name              text        not null,
    status            text        not null default 'onboarding'
                          check (status in ('onboarding', 'shadow', 'active', 'paused', 'cancelled')),
    -- Pointer to the agent version currently running. The FK lands with
    -- `agent_versions` in E2; adding it later is an additive (expand) change.
    active_version_id uuid,
    -- Rolling TTL of messages, in months. The bounds are the product decision,
    -- not a preference: retention outside 12–24 breaks the LGPD commitment.
    retention_months  smallint    not null default 12
                          check (retention_months between 12 and 24),
    never_say_ai      boolean     not null default true,
    followup_enabled  boolean     not null default false,
    primary_language  text        not null default 'pt-BR',
    -- End of the 7-day debut window, during which 100% of replies are judged.
    shadow_until      timestamptz,
    -- Starts the 10-day countdown to hard delete (ADR-12: no copy is kept).
    cancelled_at      timestamptz,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);

comment on table public.tenants is
    'One row per merchant. Tenants differ only by configuration, never by code.';

-- ---------------------------------------------------------------------------
-- 1.2 profiles — complements auth.users, which owns email/password/session
-- ---------------------------------------------------------------------------
create table public.profiles (
    user_id           uuid primary key references auth.users (id) on delete cascade,
    full_name         text,
    is_platform_admin boolean     not null default false,
    created_at        timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- 1.3 memberships — the first table with tenant_id
-- ---------------------------------------------------------------------------
create table public.memberships (
    id          uuid primary key default gen_random_uuid(),
    tenant_id   uuid        not null references public.tenants (id) on delete cascade,
    user_id     uuid        not null references auth.users (id) on delete cascade,
    role        text        not null
                    check (role in ('owner', 'manager', 'attendant')),
    -- Fine-grained permissions per RF-047.
    permissions jsonb       not null default '{}'::jsonb,
    created_at  timestamptz not null default now(),
    unique (tenant_id, user_id)
);

create index memberships_user_id_idx on public.memberships (user_id);

-- ---------------------------------------------------------------------------
-- updated_at moves by trigger, not by convention
-- ---------------------------------------------------------------------------
-- A write path that forgets to touch `updated_at` produces a column that lies
-- with authority. The trigger makes forgetting impossible; every future table
-- with an `updated_at` attaches this same function.
create function public.touch_updated_at()
    returns trigger
    language plpgsql
    set search_path = pg_catalog
as $$
begin
    new.updated_at = now();
    return new;
end
$$;

create trigger tenants_touch_updated_at
    before update on public.tenants
    for each row execute function public.touch_updated_at();

-- ---------------------------------------------------------------------------
-- Application roles (ADR-11)
-- ---------------------------------------------------------------------------
-- Two roles, two connection pools, one purpose each: `worker_role` for the
-- business tables, `sender_role` for the outbox. Neither may have BYPASSRLS and
-- neither owns the tables it reads — those two facts are what make RLS an
-- actual boundary rather than a decoration, and tests assert both.
--
-- NOLOGIN on purpose. Granting LOGIN requires a password, and a password in a
-- committed migration is a leaked secret. The login grant happens out of band,
-- per environment. Tests reach the roles with SET ROLE, which enforces RLS
-- identically: after SET ROLE the current role is a plain non-owner role.
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'worker_role') then
        create role worker_role nologin nobypassrls;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'sender_role') then
        create role sender_role nologin nobypassrls;
    end if;
end
$$;

-- The admin role has to be able to step into the app roles: this is how a
-- migration or an operator verifies a policy from the application's point of
-- view, and how the db/rls suites reach the roles without a login password.
-- Membership grants the app roles nothing — it lets `postgres`, which already
-- outranks them, assume them.
grant worker_role, sender_role to postgres;

grant usage on schema public to worker_role, sender_role;

-- The roles are granted table privileges and then scoped by RLS. Withholding
-- the GRANT would also block a cross-tenant read, but it would prove nothing
-- about RLS — and the tables these roles legitimately need are coming.
grant select                         on public.tenants     to worker_role, sender_role;
grant select, insert, update, delete on public.memberships to worker_role;
grant select                         on public.memberships to sender_role;

-- The hub reads these three through the Data API as `authenticated`, so the
-- role needs the privilege and RLS does the scoping — same reasoning as above.
-- These are merchant-facing tables; the internal ones (outbox, the pgmq queues,
-- evals) never get a grant here and never enter the exposed schemas.
grant select on public.tenants, public.profiles, public.memberships to authenticated;

-- ---------------------------------------------------------------------------
-- Row level security
-- ---------------------------------------------------------------------------
-- The policies ship in the same migration that creates the tables, on purpose:
-- a table that exists for even one migration with a GRANT and no policy is a
-- table that was readable across tenants at some point in the schema's history.
--
-- The red→green cycle that produced this still happened — the leak suite was
-- run against these tables with the GRANTs and no policies, and watched
-- returning rows of the wrong tenant through all three credentials. That
-- evidence lives in the commit message, not in a permanently insecure
-- migration.

-- Which tenants the current user belongs to.
--
-- SECURITY DEFINER is load-bearing, not convenience: a policy on `memberships`
-- that reads `memberships` would recurse forever. The function runs as the
-- table owner, for whom RLS does not apply, which breaks the cycle. Fixed
-- search_path and EXECUTE revoked from PUBLIC per ADR-11.
create function public.user_tenant_ids()
    returns setof uuid
    language sql
    stable
    security definer
    set search_path = pg_catalog, public
as $$
    select m.tenant_id from public.memberships m where m.user_id = (select auth.uid())
$$;

revoke execute on function public.user_tenant_ids() from public;
grant execute on function public.user_tenant_ids() to authenticated;

-- The tenant the current unit of work is scoped to, for the app pools.
--
-- Unset, empty or unparseable yields NULL, and `tenant_id = NULL` is never
-- true — so a pool that forgot to scope its transaction, or wrote garbage into
-- the setting, reads nothing instead of reading everything. plpgsql because a
-- bare `::uuid` cast would turn garbage into an error on every statement of
-- the connection — failing closed must not mean failing loudly forever.
create function public.current_app_tenant_id()
    returns uuid
    language plpgsql
    stable
    set search_path = pg_catalog
as $$
begin
    return nullif(current_setting('app.tenant_id', true), '')::uuid;
exception
    when invalid_text_representation then
        return null;
end
$$;

revoke execute on function public.current_app_tenant_id() from public;
grant execute on function public.current_app_tenant_id() to worker_role, sender_role;

-- tenants ---------------------------------------------------------------------
alter table public.tenants enable row level security;

create policy tenants_member_read on public.tenants
    for select to authenticated
    using (id in (select public.user_tenant_ids()));

create policy tenants_app_read on public.tenants
    for select to worker_role, sender_role
    using (id = public.current_app_tenant_id());

-- profiles --------------------------------------------------------------------
alter table public.profiles enable row level security;

create policy profiles_read_own on public.profiles
    for select to authenticated
    using (user_id = (select auth.uid()));

-- memberships -----------------------------------------------------------------
alter table public.memberships enable row level security;

create policy memberships_member_read on public.memberships
    for select to authenticated
    using (tenant_id in (select public.user_tenant_ids()));

-- USING scopes what the role may see and change; WITH CHECK scopes what it may
-- write. Without the second half, a worker scoped to tenant A could insert a
-- row belonging to tenant B — a leak in the opposite direction, which reads as
-- "no rows returned" in every test that only checks SELECT.
create policy memberships_worker_scoped on public.memberships
    for all to worker_role
    using (tenant_id = public.current_app_tenant_id())
    with check (tenant_id = public.current_app_tenant_id());

create policy memberships_sender_read on public.memberships
    for select to sender_role
    using (tenant_id = public.current_app_tenant_id());

-- SABOTAGE N4 (plano §E0-12) — desligar RLS em memberships.
-- Desligar é o que produz vazamento de verdade: remover a policy faria a
-- tabela fechar (nenhuma linha), que reprova por privilégio ausente e não
-- por vazamento — e privilégio ausente não é prova de RLS (decisão 16).
alter table public.memberships disable row level security;

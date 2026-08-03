-- E2 · S2 — the schema slice of the milestone: the agent's versions, its
-- knowledge, the evaluation trail and the alerts.
--
-- Sources: core/dicionario-de-dados.md §2.1, §2.4, §2.5, §6.1–6.5 and the
-- integrity notes of §8. Conventions from the same document: uuid PKs,
-- timestamptz, enums as text + CHECK, tenant_id + RLS on every business table.
--
-- Two decisions of the milestone are materialised here and nowhere else:
--
--   D1 (amended, decision 79) — the agent's model is per-tenant CONFIGURATION.
--   It lives in the version's own row, defaulting to `claude-sonnet-5`, so
--   changing a tenant's model is an UPDATE and never a deploy. Judge 1 is
--   deliberately absent: it is fixed by the platform (`claude-haiku-4-5`),
--   because a safety gate a customer can reconfigure is not a gate. The
--   routing trail lands in `llm_calls.provider/model` — everything goes out
--   through OpenRouter and the model varies per tenant.
--
--   D2 — the embedding is `text-embedding-3-small` at 1536 dimensions. The
--   column is typed `vector(1536)` rather than bare `vector` on purpose: the
--   dimension has to fail at INSERT, not silently poison the index later.
--
-- D6: the source of truth for "which version is running" is the partial unique
-- index below. `tenants.active_version_id` stays unused — two mechanisms
-- stating the same fact diverge on the worst possible day.

create extension if not exists vector;

-- ---------------------------------------------------------------------------
-- 2.1 agent_versions — append-only history (only status/activated_at mutate)
-- ---------------------------------------------------------------------------
create table public.agent_versions (
    id                   uuid primary key default gen_random_uuid(),
    tenant_id            uuid        not null references public.tenants (id) on delete cascade,
    -- The version this one was derived from: the navigation tree of the hub.
    -- ON DELETE SET NULL, never CASCADE — deleting a draft must not take its
    -- descendants, including the one in production, down with it.
    parent_version_id    uuid        references public.agent_versions (id) on delete set null,
    status               text        not null default 'draft'
                             check (status in ('draft', 'active', 'archived')),
    origin               text        not null
                             check (origin in ('onboarding', 'bruno', 'lojista', 'flywheel')),
    author_user_id       uuid        references auth.users (id) on delete set null,
    -- D1: per-tenant model. Not an enum — the provider's catalogue changes far
    -- faster than this schema, and OpenRouter takes the model as a string.
    -- Non-empty is enforced because "" would route nowhere and only fail at
    -- call time, far from the screen that caused it.
    model                text        not null default 'claude-sonnet-5'
                             check (length(model) > 0),
    base_prompt          text        not null,
    scenario_prompts     jsonb       not null default '{}'::jsonb,
    identity             jsonb       not null default '{}'::jsonb,
    enabled_tools        text[]      not null default '{}',
    price_policy         text        not null default 'if_asked'
                             check (price_policy in ('free', 'total_only', 'never', 'if_asked')),
    product_presentation jsonb       not null default '{}'::jsonb,
    escalation_config    jsonb       not null default '{}'::jsonb,
    scheduling_config    jsonb       not null default '{}'::jsonb,
    change_summary       text,
    created_at           timestamptz not null default now(),
    activated_at         timestamptz
);

create index agent_versions_tenant_idx on public.agent_versions (tenant_id);

-- D6 / integrity note 1: ONE active version per tenant, enforced by the
-- database. Partial, so the append-only history of drafts and archived
-- versions piles up freely — only `active` is unique. Application code cannot
-- provide this: two activations racing in two connections both read "no active
-- version" and both write one.
create unique index agent_versions_one_active_per_tenant
    on public.agent_versions (tenant_id)
    where status = 'active';

comment on column public.agent_versions.model is
    'Chat model for this tenant, routed through OpenRouter (D1, decision 79). '
    'Judge 1 is NOT configurable per tenant.';

-- ---------------------------------------------------------------------------
-- 2.5 knowledge_chunks
-- ---------------------------------------------------------------------------
create table public.knowledge_chunks (
    id         uuid primary key default gen_random_uuid(),
    tenant_id  uuid        not null references public.tenants (id) on delete cascade,
    source     text        not null check (source in ('faq', 'policy', 'upload')),
    title      text,
    content    text        not null,
    -- D2. The dimension is part of the type: a bare `vector` would accept
    -- anything and only break when the index is built.
    embedding  vector(1536),
    metadata   jsonb       not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create index knowledge_chunks_tenant_idx on public.knowledge_chunks (tenant_id);

-- Cosine, because the embeddings are normalised and cosine is what the model
-- is trained for. HNSW over IVFFlat: it needs no training pass, which matters
-- for a table that starts empty on every new tenant.
create index knowledge_chunks_embedding_idx
    on public.knowledge_chunks using hnsw (embedding vector_cosine_ops);

comment on table public.knowledge_chunks is
    'Merchant knowledge. LGPD (note 5 of the dictionary): chunks derived from a '
    'conversation enter the per-contact purge; FAQ/policy chunks do not.';

-- ---------------------------------------------------------------------------
-- 6.5 alerts — merchant-facing, so it lives in public
-- ---------------------------------------------------------------------------
create table public.alerts (
    id          uuid primary key default gen_random_uuid(),
    -- NULL = platform alert, belonging to no tenant. The app policies below
    -- scope on equality, and `tenant_id = null` is never true, so a platform
    -- alert is invisible to every tenant — which is exactly the intent.
    tenant_id   uuid        references public.tenants (id) on delete cascade,
    type        text        not null
                    check (type in ('critical_violation', 'queue_depth', 'queue_age', 'dlq',
                                    'outbox_unknown', 'outbox_review', 'meta_tier',
                                    'connector_error', 'lease_expired')),
    severity    text        not null check (severity in ('info', 'warning', 'critical')),
    title       text        not null,
    payload     jsonb       not null default '{}'::jsonb,
    status      text        not null default 'open'
                    check (status in ('open', 'acknowledged', 'resolved')),
    created_at  timestamptz not null default now(),
    resolved_at timestamptz
);

create index alerts_open_idx on public.alerts (tenant_id, created_at desc)
    where status = 'open';

-- ---------------------------------------------------------------------------
-- 2.4 scenarios and §6.1–6.4 — the evaluation trail, internal
-- ---------------------------------------------------------------------------
-- ADR-11 (and CLAUDE.md verbatim): outbox, pgmq queues and evals stay out of
-- the Data API schema. `scenarios` is born here with them; the E4 double gate
-- (the merchant tries scenarios out) will expose it as an additive change — a
-- `security_invoker` view in public plus its policy, never a table move.
create table internal.scenarios (
    id         uuid primary key default gen_random_uuid(),
    -- NULL = the global base pack maintained by the admin.
    tenant_id  uuid        references public.tenants (id) on delete cascade,
    origin     text        not null check (origin in ('base_pack', 'ai_variation', 'manual')),
    occasion   text        not null,
    title      text,
    script     jsonb       not null,
    expected   jsonb       not null default '{}'::jsonb,
    active     boolean     not null default true,
    created_at timestamptz not null default now()
);

create table internal.eval_runs (
    id               uuid primary key default gen_random_uuid(),
    tenant_id        uuid        not null references public.tenants (id) on delete cascade,
    agent_version_id uuid        not null references public.agent_versions (id) on delete cascade,
    trigger          text        not null
                         check (trigger in ('onboarding', 'manual', 'seasonal', 'flywheel')),
    status           text        not null default 'running'
                         check (status in ('running', 'done', 'failed')),
    aggregate_score  numeric(5, 2),
    summary          jsonb       not null default '{}'::jsonb,
    started_at       timestamptz not null default now(),
    finished_at      timestamptz
);

-- The high-volume logs below cascade from the conversation (integrity note 4):
-- the TTL purge and the cancelled-merchant purge have to drag their derivatives
-- with them, or the hard delete of ADR-12 leaves a trail behind.
create table internal.judge_scores (
    id              bigint primary key generated always as identity,
    tenant_id       uuid        not null references public.tenants (id) on delete cascade,
    kind            text        not null check (kind in ('pre_send', 'post_hoc')),
    conversation_id uuid        references public.conversations (id) on delete cascade,
    message_id      uuid        references public.messages (id) on delete cascade,
    eval_run_id     uuid        references internal.eval_runs (id) on delete cascade,
    scenario_id     uuid        references internal.scenarios (id) on delete set null,
    judge_model     text        not null,
    score           numeric(5, 2),
    verdict         text        not null check (verdict in ('pass', 'fail', 'critical')),
    rationale       text,
    created_at      timestamptz not null default now()
);

create index judge_scores_conversation_idx on internal.judge_scores (conversation_id);
create index judge_scores_eval_run_idx on internal.judge_scores (eval_run_id);

create table internal.tool_calls (
    id              bigint primary key generated always as identity,
    tenant_id       uuid        not null references public.tenants (id) on delete cascade,
    conversation_id uuid        not null references public.conversations (id) on delete cascade,
    message_id      uuid        references public.messages (id) on delete cascade,
    tool_name       text        not null,
    input           jsonb       not null default '{}'::jsonb,
    output          jsonb,
    success         boolean     not null,
    error           text,
    latency_ms      integer,
    created_at      timestamptz not null default now()
);

create index tool_calls_conversation_idx on internal.tool_calls (conversation_id);

create table internal.llm_calls (
    id              bigint primary key generated always as identity,
    -- NULL on platform calls (no tenant behind them).
    tenant_id       uuid        references public.tenants (id) on delete cascade,
    purpose         text        not null
                        check (purpose in ('agent_reply', 'judge_pre', 'judge_async',
                                           'prompt_generator', 'copy_variation', 'embedding')),
    conversation_id uuid        references public.conversations (id) on delete cascade,
    eval_run_id     uuid        references internal.eval_runs (id) on delete cascade,
    -- D1: the route is always OpenRouter, the model varies per tenant. Both are
    -- recorded because the cost trail has to say what was actually billed.
    provider        text        not null,
    model           text        not null,
    input_tokens    integer,
    output_tokens   integer,
    cost_usd        numeric(10, 6),
    latency_ms      integer,
    created_at      timestamptz not null default now()
);

create index llm_calls_conversation_idx on internal.llm_calls (conversation_id);
create index llm_calls_eval_run_idx on internal.llm_calls (eval_run_id);

-- ---------------------------------------------------------------------------
-- Privileges
-- ---------------------------------------------------------------------------
-- The hub reads the merchant-facing tables as `authenticated`; RLS does the
-- scoping. Writes are not granted here: versions are produced by the generator
-- (E4) and knowledge is ingested server-side, both outside the Data API.
grant select on public.agent_versions, public.knowledge_chunks, public.alerts to authenticated;

-- The runtime reads the active version and the knowledge, updates the version's
-- status when an approved run activates it (S11), and opens alerts (S8).
grant select, update         on public.agent_versions   to worker_role;
grant select                 on public.knowledge_chunks to worker_role;
grant select, insert, update on public.alerts           to worker_role;

grant select                 on internal.scenarios   to worker_role;
grant select, insert, update on internal.eval_runs   to worker_role;
grant select, insert         on internal.judge_scores to worker_role;
grant select, insert         on internal.tool_calls  to worker_role;
grant select, insert         on internal.llm_calls   to worker_role;

-- `sender_role` gets nothing at all: senders drain the outbox. They have no
-- business reading a prompt, a knowledge base or an evaluation.
--
-- The Data API roles get nothing on `internal` either. The schema-level revoke
-- of migration 0003 already covers it, and the leak suite asserts the denial.

-- ---------------------------------------------------------------------------
-- Row level security
-- ---------------------------------------------------------------------------
-- Policies ship in the same migration that creates the tables: a table that
-- exists for even one migration with a GRANT and no policy was readable across
-- tenants at some point in the schema's history.
alter table public.agent_versions   enable row level security;
alter table public.knowledge_chunks enable row level security;
alter table public.alerts           enable row level security;
alter table internal.scenarios      enable row level security;
alter table internal.eval_runs      enable row level security;
alter table internal.judge_scores   enable row level security;
alter table internal.tool_calls     enable row level security;
alter table internal.llm_calls      enable row level security;

-- agent_versions ---------------------------------------------------------------
create policy agent_versions_member_read on public.agent_versions
    for select to authenticated
    using (tenant_id in (select public.user_tenant_ids()));

-- USING scopes what may be seen and changed, WITH CHECK what may be written:
-- without the second half a worker scoped to A could move a row into B.
create policy agent_versions_worker_scoped on public.agent_versions
    for all to worker_role
    using (tenant_id = public.current_app_tenant_id())
    with check (tenant_id = public.current_app_tenant_id());

-- knowledge_chunks -------------------------------------------------------------
create policy knowledge_chunks_member_read on public.knowledge_chunks
    for select to authenticated
    using (tenant_id in (select public.user_tenant_ids()));

create policy knowledge_chunks_worker_read on public.knowledge_chunks
    for select to worker_role
    using (tenant_id = public.current_app_tenant_id());

-- alerts -----------------------------------------------------------------------
create policy alerts_member_read on public.alerts
    for select to authenticated
    using (tenant_id in (select public.user_tenant_ids()));

create policy alerts_worker_scoped on public.alerts
    for all to worker_role
    using (tenant_id = public.current_app_tenant_id())
    with check (tenant_id = public.current_app_tenant_id());

-- the evaluation trail ---------------------------------------------------------
create policy scenarios_worker_read on internal.scenarios
    for select to worker_role
    using (tenant_id = public.current_app_tenant_id());

create policy eval_runs_worker_scoped on internal.eval_runs
    for all to worker_role
    using (tenant_id = public.current_app_tenant_id())
    with check (tenant_id = public.current_app_tenant_id());

create policy judge_scores_worker_scoped on internal.judge_scores
    for all to worker_role
    using (tenant_id = public.current_app_tenant_id())
    with check (tenant_id = public.current_app_tenant_id());

create policy tool_calls_worker_scoped on internal.tool_calls
    for all to worker_role
    using (tenant_id = public.current_app_tenant_id())
    with check (tenant_id = public.current_app_tenant_id());

create policy llm_calls_worker_scoped on internal.llm_calls
    for all to worker_role
    using (tenant_id = public.current_app_tenant_id())
    with check (tenant_id = public.current_app_tenant_id());

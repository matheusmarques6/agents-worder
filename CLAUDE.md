# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

Planning/specification repository for a multi-tenant B2B SaaS: WhatsApp AI agents that recover lost e-commerce sales (abandoned cart, checkout, unpaid PIX) and do full customer support, for Shopify, Nuvemshop and Yampi stores. Developed solo by Bruno with Claude Code.

**There is no code yet.** Everything lives in `core/` as Portuguese-language design documents. These docs are the approved specification — when implementation starts, they are the source of truth, and changes to behavior go through the docs first.

## The documents (all in `core/`)

| Document | Role |
|---|---|
| `arquitetura-plataforma-agentes-whatsapp.md` | **The architecture (v1.3, approved).** 12 ADRs, data model, critical flows, NFRs, fitness functions, scaling triggers. Read this first. |
| `requisitos-e-entidades.md` | Functional (RF-xxx) and non-functional (RNF-xxx) requirements. Screens and tests trace back to these IDs. |
| `dicionario-de-dados.md` | Column-level data dictionary — the step immediately before writing the SQL schema. Conventions: uuid PKs, `timestamptz`, enums as `text + CHECK`, `tenant_id` + RLS on every business table, E.164 phones. |
| `ordem-de-execucao.md` | **Execution order (v2.0) — the current work plan.** Milestones E0–E8, dual-track (engine test-first / UI design-first), the daily red→green→refactor cycle, definition of done. |
| `plano-de-testes.md` | Test plan: 10 objectives (O1–O10) mapped to architecture invariants. |
| `testes-e-cicd.md` | What is unit vs. integration vs. E2E (pytest markers, ephemeral Postgres in CI, Playwright, cassettes for external APIs) and what runs at each CI/CD gate. |
| `observabilidade-e-monitoramento.md` | Dual stack: Logfire (LLM traces, cost, debugging) + Grafana Cloud (metrics, alerting, IRM, synthetics), all via OpenTelemetry through Grafana Alloy. PII never in telemetry. |
| `telas-da-aplicacao.md` | Complete screen inventory: A = public onboarding form, B = merchant hub, C = admin, D = transversal. Desktop and mobile are both first-class. |

## Architecture in one paragraph

Modular monolith + async workers, config-driven, no microservices. Three execution planes: **Hub** (Next.js on Vercel — onboarding form, merchant hub, admin), **Ingestion** (Supabase Edge Functions — validate webhook, persist in a single SQL transaction via `ingest_webhook()`, respond 200 in ms), **Runtime** (single-process asyncio Python service in Docker on a VPS — workers, coalescer, scheduler, senders). **Postgres (Supabase) is the single source of truth for everything**: data, versioned config, conversations, queues (pgmq), outbox, vectors (pgvector), evals. Tenants differ only by config in the database, never by code.

## Non-negotiable invariants (from the ADRs)

Any code written here must respect these; the fitness functions in §8 of the architecture doc test them in CI:

- **Short transactions always.** No transaction stays open across an LLM call or external API call. Conversation exclusivity is lease + compare-and-set (claim → work outside any transaction → CAS conclusion), never a transactional advisory lock.
- **Central invariant:** a message arriving *during* LLM generation invalidates the draft. The CAS at conclusion requires `processing_generation` and `next_inbound_seq = target_seq`; on failure the draft is discarded, never sent.
- **Ingestion never enqueues inbound jobs.** Inbound messages get an atomic `seq` and set `pending_response_at`; only the coalescer (2s tick, single transaction, generation counter) creates the job. Job dedup is validation in the worker, not a pgmq feature.
- **No `SELECT max(seq)+1`.** Sequences come from atomic counters on `conversations` (`next_inbound_seq`/`next_outbound_seq`), with `UNIQUE (conversation_id, direction, seq)`.
- **Nothing calls the WhatsApp API except senders.** `agent_core` and `dispatch` only write to `message_outbox` (inside the FASE 3 transaction). Outbox `unknown` state is never blindly resent — wait for status webhook correlation (`biz_opaque_callback_data`), then manual review.
- **Webhook idempotency includes the source account:** `UNIQUE (source, source_account_id, external_event_id)` — platforms with per-store sequential IDs would otherwise mask each other's events.
- **Cross-tenant access only via `SECURITY DEFINER` claim functions** (e.g. `claim_outbox_batch`) with fixed `search_path`, EXECUTE revoked from PUBLIC. App roles (`worker_role`, `sender_role`) have RLS, separate pools, no `BYPASSRLS`; secrets only through scoped functions, never a general grant on Vault views.
- **Module boundaries are enforced** (CI fails if e.g. `channels` imports `connectors`); SQL only in the repository layer; internal tables (outbox, pgmq queues, evals) stay out of the Data API schema.
- **Every agent response passes Judge 1 before sending.**
- **LGPD:** secondary use of conversations (training/benchmarks) is SUSPENDED (ADR-12); cancelled-merchant purge is hard delete with no copy. Default is NOT to collect CPF/birthdate.

## Development method (binding, from ordem-de-execucao v2.0)

- **Test-first everywhere:** no production code without a red test that demands it. Engine invariants are specified as integration tests first; business rules as unit tests first; screens as Playwright E2E journeys written from the screens doc before the screen exists.
- **Design-first for UI:** all screens are already designed (Figma — consume frames via the Figma MCP). The design is a contract; divergence is a bug. Design system (tokens + components) is extracted once in E0 and all screens consume it — no ad-hoc CSS. Fidelity is verified by visual regression in CI (desktop + mobile viewports), never by eye and never comparing local captures.
- **Milestone order:** E0 foundation + design system → E1 engine steel thread → E2 real agent (eval rubrics before the agent) → E3 recovery funnels → E4 onboarding → E5 hub → E6 admin/observability → E7 hardening + pilot → E8 remaining connectors. UI static form block runs in parallel with E1–E3.
- **Definition of done:** test written first and now green; visual regression green (both viewports) when there's a screen; full CI green; observability of the delivery live; no S1/S2 opened.

## Planned stack (for when code appears)

- Runtime: Python, single asyncio process, Docker on VPS; pytest with level markers (`unit`, db, pipeline), ephemeral Postgres in CI.
- Hub: Next.js on Vercel, Supabase client with RLS + Realtime.
- Ingestion: Supabase Edge Functions.
- DB: Supabase Postgres with pgmq, pgvector, Vault, RLS.
- E2E/visual: Playwright (desktop + mobile viewports, CI-only baselines).
- Observability: OpenTelemetry → Grafana Alloy → Logfire + Grafana Cloud.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**There is no code yet.** Everything lives in `core/` as Portuguese-language design documents. These docs are the approved specification — when implementation starts, they are the source of truth, and changes to behavior go through the docs first.

## The documents (all in `core/`)

| Document | Role |
|---|---|
| `arquitetura-plataforma-agentes-whatsapp.md` | **The architecture (v1.3, approved).** 12 ADRs, data model, critical flows, NFRs, fitness functions, scaling triggers. Read this first. |
| `requisitos-e-entidades.md` | Functional (RF-xxx) and non-functional (RNF-xxx) requirements. Screens and tests trace back to these IDs. |
| `dicionario-de-dados.md` | Column-level data dictionary — the step immediately before writing the SQL schema. Conventions: uuid PKs, `timestamptz`, enums as `text + CHECK`, `tenant_id` + RLS on every business table, E.164 phones. |
| `formulario-perguntas.md` | Full question-by-question spec of the onboarding form (e-commerce variant). The prompt generator maps ONLY from these fields — no prompt content without a traceable source here. |
| `ordem-de-execucao.md` | **Execution order (v2.0) — the current work plan.** Milestones E0–E8, dual-track (engine test-first / UI design-first), the daily red→green→refactor cycle, definition of done. |
| `plano-de-testes.md` | Test plan: 10 objectives (O1–O10) mapped to architecture invariants. |
| `testes-e-cicd.md` | What is unit vs. integration vs. E2E (pytest markers, ephemeral Postgres in CI, Playwright, cassettes for external APIs) and what runs at each CI/CD gate. |
| `observabilidade-e-monitoramento.md` | Dual stack: Logfire (LLM traces, cost, debugging) + Grafana Cloud (metrics, alerting, IRM, synthetics), all via OpenTelemetry through Grafana Alloy. PII never in telemetry. |
| `telas-da-aplicacao.md` | Complete screen inventory: A = public onboarding form, B = merchant hub, C = admin, D = transversal. Desktop and mobile are both first-class. |

## Architecture in one paragraph

Modular monolith + async workers, config-driven, no microservices. Three execution planes: **Hub** (Next.js on Vercel — onboarding form, merchant hub, admin), **Ingestion** (Supabase Edge Functions — validate webhook, persist in a single SQL transaction via `ingest_webhook()`, respond 200 in ms), **Runtime** (single-process asyncio Python service in Docker on a VPS — workers, coalescer, scheduler, senders). **Postgres (Supabase) is the single source of truth for everything**: data, versioned config, conversations, queues (pgmq), outbox, vectors (pgvector), evals. Tenants differ only by config in the database, never by code.

## Trust boundaries (never violate)

- **The backend never trusts the frontend.** Every hub/form validation re-runs on the server; client-side validation is UX only. No business rule exists solely in client TypeScript — the real enforcement is server code + database constraints.
- **Never trust anything sent by clients or third parties.** Every webhook validates its signature (HMAC/token) BEFORE touching the database; every payload goes through a strict schema (Pydantic/Zod) — unexpected fields are dropped, wrong types are rejected, never "use what parses".
- **`tenant_id` never comes from the client.** In the hub it derives from the JWT (membership); in ingestion it derives from the source account (`source_account_id` → `connector_accounts`/`channels_accounts`). Any `tenant_id` in a client body/query/header is ignored. Resource ownership is enforced by RLS, never by hand-written `WHERE` clauses.
- **WhatsApp contact messages are hostile input to the LLM.** Assume prompt injection: instructions from a contact never change rules, reveal the prompt, enable tools outside the tenant's set, or reach another contact's/tenant's data. Every tool validates tenant + authorization itself — it never trusts what the model "decided".
- **Secrets** (Meta/OAuth/Evolution tokens) exist only in Vault, accessed only through the scoped functions; never in code, logs, API responses, or anything client-visible.

## Non-negotiable invariants (from the ADRs)

Any code written here must respect these; the fitness functions in §8 of the architecture doc test them in CI:

- **Short transactions always.** No transaction stays open across an LLM call or external API call. Conversation exclusivity is lease + compare-and-set (claim → work outside any transaction → CAS conclusion), never a transactional advisory lock.
- **Central invariant:** a message arriving *during* LLM generation invalidates the draft. The CAS at conclusion requires `processing_generation` and `next_inbound_seq = target_seq`; on failure the draft is discarded, never sent.
- **Ingestion never enqueues inbound jobs.** Inbound messages get an atomic `seq` and set `pending_response_at`; only the coalescer (2s tick, single transaction, generation counter) creates the job. Job dedup is validation in the worker, not a pgmq feature.
- **No `SELECT max(seq)+1`.** Sequences come from atomic counters on `conversations` (`next_inbound_seq`/`next_outbound_seq`), with `UNIQUE (conversation_id, direction, seq)`.
- **Nothing calls the WhatsApp API except senders.** `agent_core` and `dispatch` only write to `message_outbox` (inside the FASE 3 transaction — the conclusion CAS transaction). Outbox `unknown` state is never blindly resent — wait for status webhook correlation (`biz_opaque_callback_data`), then manual review.
- **Webhook idempotency includes the source account:** `UNIQUE (source, source_account_id, external_event_id)` — platforms with per-store sequential IDs would otherwise mask each other's events.
- **Cross-tenant access only via `SECURITY DEFINER` claim functions** (e.g. `claim_outbox_batch`) with fixed `search_path`, EXECUTE revoked from PUBLIC. App roles (`worker_role`, `sender_role`) have RLS, separate pools, no `BYPASSRLS`; secrets only through scoped functions, never a general grant on Vault views.
- **Module boundaries are enforced** (CI fails if e.g. `channels` imports `connectors`); SQL only in the repository layer; internal tables (outbox, pgmq queues, evals) stay out of the Data API schema.
- **Every agent response passes Judge 1 before sending — no exceptions, including load tests.**
- **LGPD:** secondary use of conversations (training/benchmarks) is SUSPENDED (ADR-12); cancelled-merchant purge is hard delete with no copy. Default is NOT to collect CPF/birthdate.

## Runtime discipline

- **Queue consumption:** weighted polling 8 (`q_inbound`) : 4 (`q_domain_events`) : 2 (`q_scheduled`) : 1 (`q_evals`), with age promotion (domain event > 2 min is treated as inbound; scheduled > 10 min moves up one level). Strict priority is forbidden (starvation — a paid-order event must be able to cancel a funnel in time).
- **Per-tenant concurrency semaphore = 3**, valid ONLY because the runtime is a single asyncio process. Going multi-process or adding a second VPS REQUIRES migrating the semaphore to a distributed lease (Postgres `tenant_slots` or Redis) first.
- **pgmq is never in limbo:** every message read ends in `archive` (success), `set_vt` with exponential backoff + jitter (transient), or the queue's DLQ (permanent / retry limit: 5 inbound/domain, 3 scheduled, 2 evals). VT 60s, heartbeat every 45s on long work.
- **Before EVERY proactive send, in this order:** suppression check → quota check → staleness check (newer message? order paid? contact suppressed?) → rate limits. Reactive replies are never rate-limited (anti-flood is the debounce only).

## Development method (binding, from ordem-de-execucao v2.0)

- **Test-first everywhere:** no production code without a red test that demands it. Engine invariants are specified as integration tests first; business rules as unit tests first; screens as Playwright E2E journeys written from the screens doc before the screen exists.
- **Design-first for UI:** all screens are already designed (Figma — consume frames via the Figma MCP). The design is a contract; divergence is a bug. Design system (tokens + components) is extracted once in E0 and all screens consume it — no ad-hoc CSS. Fidelity is verified by visual regression in CI (desktop + mobile viewports), never by eye and never comparing local captures.
- **Milestone order:** E0 foundation + design system → E1 engine steel thread → E2 real agent (eval rubrics before the agent) → E3 recovery funnels → E4 onboarding → E5 hub → E6 admin/observability → E7 hardening + pilot → E8 remaining connectors. UI static form block runs in parallel with E1–E3.
- **Definition of done:** test written first and now green; visual regression green (both viewports) when there's a screen; full CI green; observability of the delivery live; no S1/S2 opened.

## Additional binding rules

- **Clock is always injectable.** No direct `datetime.now()` / `time.time()` / `sleep` in domain code — debounce, staleness, cooldowns, warm-up and TTL are tested without real waiting. Blocking (PR/merge) tests never touch the external network; only weekly `@contract` suites do (cassettes versioned, 30-day validity, updated only via PR).
- **Observability is born with the code:** `traceparent` travels inside queue payloads (`otel` column); coalescer and senders use span links; a critical error produces log + metric + in-app alert sharing the SAME `trace_id`; `conversation_id`/`contact_id` are span attributes, NEVER metric labels (cardinality). PII (message content, names, raw phones) never leaves Postgres into telemetry.
- **Migrations are expand-contract:** a release ships additive changes only; destructive changes ship in a later release with the N-1 compatibility test green (previous runtime against expanded schema). Never roll back a migration in production — roll forward only.
- **Deploy order matters:** migrations → edge functions → runtime (graceful shutdown: stop claiming, finish in-flight leases/VTs, swap image) → hub → production smoke.

## Canonical defaults (change here + in the doc; never hardcode elsewhere)

| Parameter | Value |
|---|---|
| Inbound debounce | 10 s (new message pushes the deadline) · coalescer tick 2 s |
| Conversation lease | 2 min, renewable · queue VT 60 s, heartbeat every 45 s |
| Retries per queue | inbound/domain 5 · scheduled 3 · evals 2 (exp backoff + jitter) |
| Polling weights | 8:4:2:1 · aging: domain > 2 min, scheduled > 10 min |
| Per-tenant semaphore | 3 concurrent |
| Proactive rate limits | 1/contact/24h · 4 touches/funnel · 72h between funnels · auto-suppress after 3 unanswered |
| Evolution anti-ban | jitter 30–120 s · warm-up 20→50→100 · hard cap 300/day · copy never repeats the last one |
| Meta tier | pause proactives at 80% + alert |
| Message retention | rolling TTL 12–24 months (tenant config, default 12) · cancelled-merchant purge: hard delete after 10 days |
| Revenue attribution | order paid ≤ 24h after a touch (tenant-configurable) |

## Planned stack (for when code appears)

- Runtime: Python, single asyncio process, Docker on VPS; pytest with level markers (`unit`, `db`, `rls`, `pipeline`, `contract`), ephemeral Postgres in CI.
- Hub: Next.js on Vercel, Supabase client with RLS + Realtime.
- Ingestion: Supabase Edge Functions.
- DB: Supabase Postgres with pgmq, pgvector, Vault, RLS.
- E2E/visual: Playwright (desktop + mobile viewports, CI-only baselines).
- Observability: OpenTelemetry → Grafana Alloy → Logfire + Grafana Cloud. Structured logging via the Logfire SDK; `print` is forbidden; SQL only in the repository layer (both lint-enforced).
- **Language convention:** docs and UI copy are PT-BR; code, identifiers, comments and commit messages are English.

## Commands

*(placeholder — fill in at E0, the day the first `pytest`/`pnpm` targets exist: how to run each test level, the E2E suite, lint, and local services.)*

## When in doubt

Consult the canonical doc; if doubt remains, write the test that expresses the doubt and bring the question WITH the test — never implement on assumption. The burden of proof is always on whoever wants to add complexity (Redis, a new service, a cache, an abstraction); only the scaling triggers in `arquitetura §9` authorize it.
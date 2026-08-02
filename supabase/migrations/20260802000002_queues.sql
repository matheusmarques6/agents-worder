-- E0-08 · The first queue.
--
-- Scope is one queue, `q_inbound`, because that is what the first `pipeline`
-- test demands. The other three of arquitetura §2 (`q_domain_events`,
-- `q_scheduled`, `q_evals`) and the DLQs land in E1 together with the weighted
-- polling that gives them meaning — a queue nothing reads is a queue nothing
-- tests.
--
-- INVARIANT (ADR-11): the pgmq schema is internal. It is not in the exposed
-- schemas of supabase/config.toml and the Data API roles get no privilege on
-- it here. `tests/db/test_queue_is_internal.py` fails if that ever changes.

create extension if not exists pgmq;

select pgmq.create('q_inbound');

comment on table pgmq.q_q_inbound is
    'Inbound jobs, created ONLY by the coalescer (ADR-05). Ingestion never enqueues.';

-- ---------------------------------------------------------------------------
-- Who may drive the queue
-- ---------------------------------------------------------------------------
-- pgmq's functions run as the caller, so reading a job needs table privileges,
-- not just EXECUTE: `read` updates the row's visibility timeout, `archive`
-- moves it into the archive table. `worker_role` gets exactly that and nothing
-- else — no ownership, no BYPASSRLS, same as in migration 0001.
--
-- `sender_role` is deliberately absent: senders drain the outbox, they do not
-- consume jobs.
grant usage on schema pgmq to worker_role;

grant select on pgmq.meta to worker_role;
grant select, insert, update, delete on pgmq.q_q_inbound to worker_role;
grant select, insert                 on pgmq.a_q_inbound to worker_role;
grant usage, select on all sequences in schema pgmq to worker_role;

-- ---------------------------------------------------------------------------
-- Who may not
-- ---------------------------------------------------------------------------
-- Explicit, not inherited: the extension's defaults are not this project's
-- promise, and a future `grant ... to authenticated` written by habit would
-- otherwise hand every queued job to any logged-in merchant.
revoke all on schema pgmq from anon, authenticated, service_role;
revoke all on all tables in schema pgmq from anon, authenticated, service_role;
revoke all on all functions in schema pgmq from anon, authenticated, service_role;

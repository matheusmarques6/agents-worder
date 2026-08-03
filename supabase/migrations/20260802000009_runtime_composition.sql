-- E1 · Cenários A (PR 2a) — the otel slot and the process heartbeat.
--
-- Language note, recorded where it bites: migrations 0005–0008 drifted into
-- Portuguese comments, against the CLAUDE.md convention. The decision (estado,
-- decisão 53) is that the law stands — new code returns to English; the
-- Portuguese survivors get translated opportunistically when next touched,
-- never as a wholesale diff of comments.

-- ---------------------------------------------------------------------------
-- The traceparent slot (CLAUDE.md: "traceparent travels inside queue payloads")
-- ---------------------------------------------------------------------------
-- Empty until T4 wires OpenTelemetry through the process. The slot exists NOW
-- because adding it later would change the job contract a third time — and the
-- whole point of the E0-14 lesson is that contracts change loudly, in tests.
alter type internal.coalesced_job add attribute otel jsonb;

create or replace function internal.coalesce_due_conversations(
    p_queue text default 'q_inbound',
    p_limit integer default 100
)
    returns setof internal.coalesced_job
    language plpgsql
    security definer
    set search_path = pg_catalog, public, internal
as $$
declare
    v_jobs internal.coalesced_job[];
    v_job  internal.coalesced_job;
begin
    with due as (
        select id
          from public.conversations
         where pending_response_at is not null
           and pending_response_at <= now()
         order by pending_response_at
         for update skip locked
         limit p_limit
    ),
    bumped as (
        update public.conversations c
           set processing_generation = c.processing_generation + 1,
               pending_response_at = null
          from due
         where c.id = due.id
        returning c.id, c.processing_generation, c.next_inbound_seq, c.tenant_id
    )
    select array_agg(
               row(id, processing_generation, next_inbound_seq, tenant_id, null)
                   ::internal.coalesced_job
           )
      into v_jobs
      from bumped;

    foreach v_job in array coalesce(v_jobs, array[]::internal.coalesced_job[])
    loop
        perform pgmq.send(
            p_queue,
            jsonb_build_object(
                'conversation_id', v_job.conversation_id,
                'generation', v_job.generation,
                'target_seq', v_job.target_seq,
                'tenant_id', v_job.tenant_id,
                'otel', v_job.otel
            )
        );
        return next v_job;
    end loop;
end
$$;

-- ---------------------------------------------------------------------------
-- The process heartbeat — proof 3 of the milestone, born observable
-- ---------------------------------------------------------------------------
-- One row per named process, upserted on a tick. "Is the runtime alive?" is a
-- query anyone can run, and the ≤ 3 min bound of the milestone proof is an
-- assertion against `beat_at`, not a hope.
create table internal.runtime_heartbeats (
    process_name text primary key,
    started_at   timestamptz not null default now(),
    beat_at      timestamptz not null default now()
);

comment on table internal.runtime_heartbeats is
    'Liveness of the single asyncio process (ADR-2). The T4 alert "heartbeat older than 3 min" reads this table.';

grant select, insert, update on internal.runtime_heartbeats to worker_role;

revoke all on internal.runtime_heartbeats from anon, authenticated, service_role;

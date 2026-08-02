-- E1 · A4 — o coalescer.
--
-- O par que fecha com a A1: a ingestão marca o prazo, isto transforma prazo em
-- job. É a ÚNICA origem de job de entrada no sistema, e a transação única é o
-- que faz cinco mensagens em rajada virarem um processamento só.
--
-- Ou tudo comita — a geração subiu, o job existe, o prazo foi limpo — ou nada:
-- o prazo continua vencido e o próximo tique tenta de novo. O estado que não
-- pode existir é "perdeu o prazo sem ganhar job", porque essa é a mensagem que
-- ninguém responde.

create type internal.coalesced_job as (
    conversation_id uuid,
    generation      integer,
    target_seq      integer
);

comment on type internal.coalesced_job is
    'O payload do job de entrada. `target_seq` é o último seq de entrada no instante do enfileiramento — é contra ele que o CAS da conclusão compara para descobrir se chegou mensagem nova durante a geração.';

-- `security definer` pelo mesmo motivo da ingestão, e é o padrão de claim
-- function do ADR-11: um processo coalesce para todos os tenants, então a
-- varredura é cross-tenant por natureza. Sob `worker_role` com
-- `app.tenant_id`, a RLS mostraria só um tenant e as conversas de todos os
-- outros ficariam esperando para sempre.
create function internal.coalesce_due_conversations(
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
    -- Um comando só: seleciona as vencidas, sobe a geração e limpa o prazo.
    --
    -- `SKIP LOCKED` não está aqui para evitar job duplicado — em READ
    -- COMMITTED o segundo coalescer reavaliaria a linha depois do commit do
    -- primeiro e a veria fora do WHERE de qualquer jeito. Está aqui para que o
    -- segundo **não espere**: sem ele, dois tiques concorrentes viram uma fila
    -- de bloqueio, e o tique de 2s deixa de ser 2s.
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
        returning c.id, c.processing_generation, c.next_inbound_seq
    )
    select array_agg(
               row(id, processing_generation, next_inbound_seq)::internal.coalesced_job
           )
      into v_jobs
      from bumped;

    -- O envio acontece na MESMA transação. Se a fila não existir, ou o pgmq
    -- recusar, a exceção sobe e desfaz o bump e a limpeza junto — que é
    -- exatamente o comportamento desejado.
    foreach v_job in array coalesce(v_jobs, array[]::internal.coalesced_job[])
    loop
        perform pgmq.send(
            p_queue,
            jsonb_build_object(
                'conversation_id', v_job.conversation_id,
                'generation', v_job.generation,
                'target_seq', v_job.target_seq
            )
        );
        return next v_job;
    end loop;
end
$$;

-- PERGUNTA REGISTRADA, não decidida aqui: conversas em `state = 'humano'`
-- (takeover) hoje são coalescidas como qualquer outra, e o worker é quem
-- decidirá não responder. Filtrar aqui seria implementar uma regra de produto
-- cujo teste ainda não existe — a decisão pertence ao E5, com a tela de
-- takeover na mão.

revoke execute on function internal.coalesce_due_conversations(text, integer) from public;

-- É o processo do runtime que coalesce.
grant execute on function internal.coalesce_due_conversations(text, integer) to worker_role;

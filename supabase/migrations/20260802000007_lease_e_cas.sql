-- E1 · A3 — lease e o compare-and-set estendido. O invariante central.
--
-- Três fases, transações curtas, LLM fora de qualquer transação (ADR-6). Desta
-- unidade são a primeira e a terceira; a segunda é trabalho, e trabalho não
-- mora no banco.
--
-- O que o CAS estendido existe para impedir: uma mensagem que chega DURANTE a
-- geração invalida o rascunho. A ingestão incrementa `next_inbound_seq`; como a
-- conclusão exige `next_inbound_seq = target_seq` — o retrato que o coalescer
-- tirou —, a resposta desatualizada nunca sai. O job novo responde ao conjunto
-- completo.

-- ---------------------------------------------------------------------------
-- O job passa a carregar o tenant
-- ---------------------------------------------------------------------------
-- DECISÃO REGISTRADA (rota (a) do plano): `tenant_id` entra no payload do job.
--
-- O worker precisa do tenant ANTES de tudo: sem `SET LOCAL app.tenant_id` a RLS
-- não deixa ele nem ler a conversa, e o semáforo por tenant precisa saber de
-- quem é o trabalho antes do claim. As alternativas eram descobrir o tenant por
-- uma função `security definer` — o que abriria uma segunda porta cross-tenant
-- só para responder uma pergunta que o coalescer já sabia — ou deixar o claim
-- ser `definer`, o que tiraria a RLS justamente do caminho mais quente.
--
-- Aditivo: o atributo entra no fim do tipo. Nada persiste este tipo (ele só
-- descreve um retorno), mas trocar a ordem seria uma mudança destrutiva de
-- contrato para economizar estética.
alter type internal.coalesced_job add attribute tenant_id uuid;

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
               row(id, processing_generation, next_inbound_seq, tenant_id)
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
                'tenant_id', v_job.tenant_id
            )
        );
        return next v_job;
    end loop;
end
$$;

-- ---------------------------------------------------------------------------
-- FASE 1 · claim
-- ---------------------------------------------------------------------------
-- Transação curta e commit imediato. Sem linha devolvida significa "outra task
-- está processando" — e a resposta certa a isso é desistir com um `set_vt`
-- curto, não esperar. Esperar seria segurar uma conexão do pool por dois
-- minutos para descobrir o que já se sabe agora.
--
-- `security invoker`: quem chama carrega o escopo do tenant, e a RLS impede que
-- um worker mal escopado reivindique conversa alheia.
create function internal.claim_conversation(
    p_conversation_id uuid,
    p_token uuid,
    p_lease interval default interval '2 minutes'
)
    returns table (last_processed_seq integer, version integer)
    language sql
    set search_path = pg_catalog, public
as $$
    update public.conversations
       set processing_token = p_token,
           processing_until = now() + p_lease
     where id = p_conversation_id
       -- Lease vencida é lease livre: é assim que o trabalho de um processo que
       -- morreu volta a ser feito, sem ninguém precisar limpar nada.
       and (processing_until is null or processing_until < now())
    returning last_processed_seq, version
$$;

-- Heartbeat: processamento longo renova a lease antes de ela vencer. Só o dono
-- renova — renovar a lease de outro é roubar a conversa no meio da resposta.
create function internal.renew_lease(
    p_conversation_id uuid,
    p_token uuid,
    p_lease interval default interval '2 minutes'
)
    returns boolean
    language plpgsql
    set search_path = pg_catalog, public
as $$
begin
    update public.conversations
       set processing_until = now() + p_lease
     where id = p_conversation_id
       and processing_token = p_token;

    return found;
end
$$;

-- Liberação depois de um CAS que falhou: solta a lease APENAS se o token ainda
-- for o dono. Sem essa condição, um worker atrasado liberaria a lease que um
-- segundo worker acabou de adquirir, e os dois responderiam.
create function internal.release_lease(p_conversation_id uuid, p_token uuid)
    returns boolean
    language plpgsql
    set search_path = pg_catalog, public
as $$
begin
    update public.conversations
       set processing_token = null,
           processing_until = null
     where id = p_conversation_id
       and processing_token = p_token;

    return found;
end
$$;

-- ---------------------------------------------------------------------------
-- FASE 3 · conclusão
-- ---------------------------------------------------------------------------
create type internal.turn_outcome as (
    committed    boolean,
    outbound_seq integer,
    outbox_id    uuid
);

comment on type internal.turn_outcome is
    'committed=false significa que o CAS falhou: o rascunho morre e NADA foi gravado. O chamador não reenvia, não tenta de novo com os mesmos dados — o job novo responderá ao conjunto completo.';

-- As quatro condições, e o que cada uma protege:
--
--   processing_token      — sou eu quem está segurando esta conversa;
--   version               — nada mudou na conversa desde que eu li;
--   processing_generation — meu job ainda é o corrente (o coalescer não gerou
--                           outro por cima de mim);
--   next_inbound_seq      — NENHUMA mensagem nova chegou enquanto eu escrevia.
--
-- A última é a razão de o ADR-6 existir na versão 1.3. As outras três detectam
-- concorrência entre workers; essa detecta concorrência com o CLIENTE, que é a
-- que produz a resposta constrangedora — responder à pergunta anterior depois
-- de a pessoa já ter mandado mais três.
create function internal.conclude_turn(
    p_conversation_id  uuid,
    p_token            uuid,
    p_expected_version integer,
    p_generation       integer,
    p_target_seq       integer,
    p_content          jsonb,
    p_idempotency_key  text,
    p_kind             text default 'reply'
)
    returns internal.turn_outcome
    language plpgsql
    set search_path = pg_catalog, public, internal
as $$
declare
    v_tenant_id  uuid;
    v_contact_id uuid;
    v_channel_id uuid;
    v_channel    text;
    v_seq        integer;
    v_outbox_id  uuid;
begin
    update public.conversations
       set last_processed_seq = p_target_seq,
           processing_token = null,
           processing_until = null,
           version = version + 1
     where id = p_conversation_id
       and processing_token = p_token
       and version = p_expected_version
       and processing_generation = p_generation
       and next_inbound_seq = p_target_seq
    returning tenant_id, contact_id, channel_account_id
      into v_tenant_id, v_contact_id, v_channel_id;

    if not found then
        -- O rascunho morre aqui, e morrer é o comportamento correto. A lease
        -- fica para quem for o dono dela; quem chamou usa `release_lease`, que
        -- só solta se ainda for ele.
        return row(false, null, null)::internal.turn_outcome;
    end if;

    select case type when 'cloud' then 'whatsapp_cloud' else 'whatsapp_evolution' end
      into v_channel
      from public.channels_accounts
     where id = v_channel_id;

    -- Tudo daqui para baixo comita com o UPDATE acima ou não comita nada. Uma
    -- conclusão sem linha na outbox é a versão outbound do "prazo perdido sem
    -- job": a conversa avança, o cliente nunca recebe, e nada aparece em lugar
    -- nenhum.
    insert into internal.message_outbox
        (tenant_id, conversation_id, contact_id, channel_account_id,
         kind, payload, idempotency_key)
    values
        (v_tenant_id, p_conversation_id, v_contact_id, v_channel_id,
         p_kind, p_content, p_idempotency_key)
    returning id into v_outbox_id;

    v_seq := internal.next_message_seq(p_conversation_id, 'outbound');

    insert into public.messages
        (tenant_id, conversation_id, direction, seq, channel,
         author_type, content, outbox_id)
    values
        (v_tenant_id, p_conversation_id, 'outbound', v_seq, v_channel,
         'agent', p_content, v_outbox_id);

    return row(true, v_seq, v_outbox_id)::internal.turn_outcome;
end
$$;

-- ---------------------------------------------------------------------------
-- Quem executa
-- ---------------------------------------------------------------------------
revoke execute on function internal.claim_conversation(uuid, uuid, interval) from public;
revoke execute on function internal.renew_lease(uuid, uuid, interval) from public;
revoke execute on function internal.release_lease(uuid, uuid) from public;
revoke execute on function
    internal.conclude_turn(uuid, uuid, integer, integer, integer, jsonb, text, text) from public;

grant execute on function internal.claim_conversation(uuid, uuid, interval) to worker_role;
grant execute on function internal.renew_lease(uuid, uuid, interval) to worker_role;
grant execute on function internal.release_lease(uuid, uuid) to worker_role;
grant execute on function
    internal.conclude_turn(uuid, uuid, integer, integer, integer, jsonb, text, text)
    to worker_role;

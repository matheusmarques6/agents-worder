-- E1 · A1 + A2 — o contador de sequência e a ingestão atômica.
--
-- Duas funções e um papel. Juntas elas são a porta de entrada do motor: tudo
-- que chega de fora do sistema passa por aqui, uma vez, numa transação.

-- ---------------------------------------------------------------------------
-- A2 · o contador de sequência (ADR-6b)
-- ---------------------------------------------------------------------------
-- `SELECT max(seq)+1` é proibido, e não por gosto: entre o SELECT e o INSERT
-- cabe outra conexão fazendo o mesmo, e as duas gravam o mesmo número. O
-- UPDATE ... RETURNING é atômico por construção — a segunda conexão espera a
-- primeira comitar e lê o valor já incrementado.
--
-- `security invoker` de propósito: quem chama carrega o seu escopo de RLS, e um
-- worker não consegue mexer no contador de outro tenant. A ingestão, que
-- precisa atravessar tenants, é `security definer` — mas é ELA que resolve o
-- tenant, e é por isso que pode.
create function internal.next_message_seq(p_conversation_id uuid, p_direction text)
    returns integer
    language plpgsql
    set search_path = pg_catalog, public
as $$
declare
    v_seq integer;
begin
    if p_direction not in ('inbound', 'outbound') then
        raise exception 'direção desconhecida: %', p_direction;
    end if;

    update public.conversations
       set next_inbound_seq  = next_inbound_seq  + (p_direction = 'inbound')::int,
           next_outbound_seq = next_outbound_seq + (p_direction = 'outbound')::int
     where id = p_conversation_id
    returning case p_direction
                  when 'inbound' then next_inbound_seq
                  else next_outbound_seq
              end
      into v_seq;

    -- Sem linha: a conversa não existe, ou existe e a RLS não a mostra para
    -- quem chamou. Falhar alto — devolver NULL faria o INSERT seguinte violar
    -- o NOT NULL longe da causa.
    if v_seq is null then
        raise exception 'conversa inexistente ou fora do escopo: %', p_conversation_id;
    end if;

    return v_seq;
end
$$;

revoke execute on function internal.next_message_seq(uuid, text) from public;
grant execute on function internal.next_message_seq(uuid, text) to worker_role;

-- ---------------------------------------------------------------------------
-- A1 · a ingestão
-- ---------------------------------------------------------------------------
create type internal.ingest_outcome as (
    status           text,
    webhook_event_id bigint,
    tenant_id        uuid,
    conversation_id  uuid,
    message_seq      integer
);

comment on type internal.ingest_outcome is
    'status: ingested | duplicate | unknown_account. O chamador decide o HTTP; a função não conhece HTTP.';

-- Uma chamada, uma transação, dois ramos.
--
-- SECURITY DEFINER é a exigência do desenho, não conveniência: a função precisa
-- escrever antes de saber de qual tenant é o dado — porque descobrir isso é
-- justamente o trabalho dela. É o "claim function" do ADR-11, com search_path
-- fixo e EXECUTE revogado de PUBLIC.
--
-- O que ela NÃO faz: validar formato de payload. Isso é da Edge Function que a
-- chama, com schema estrito, e chega junto com ela. Aqui só existe o que o
-- banco pode garantir sozinho — atomicidade, unicidade e a resolução do tenant.
create function internal.ingest_webhook(
    p_source            text,
    p_source_account_id text,
    p_external_event_id text,
    p_event_type        text,
    p_payload           jsonb,
    -- O debounce canônico é 10s (CLAUDE.md). Parâmetro com default para a suíte
    -- de pipeline rodar com valores minúsculos em vez de esperar de verdade.
    p_debounce          interval default interval '10 seconds'
)
    returns internal.ingest_outcome
    language plpgsql
    security definer
    set search_path = pg_catalog, public, internal
as $$
declare
    v_tenant_id          uuid;
    v_channel_account_id uuid;
    v_event_id           bigint;
    v_contact_id         uuid;
    v_conversation_id    uuid;
    v_seq                integer;
    v_phone              text;
begin
    -- 1 · O tenant vem da CONTA DE ORIGEM. Nunca do payload — um `tenant_id`
    --     no corpo do webhook é dado de terceiro, e dado de terceiro não
    --     escolhe de quem é a conversa.
    if p_source in ('shopify', 'nuvemshop', 'yampi') then
        select tenant_id
          into v_tenant_id
          from public.connector_accounts
         where platform = p_source
           and source_account_id = p_source_account_id;

    elsif p_source in ('meta', 'evolution') then
        select id, tenant_id
          into v_channel_account_id, v_tenant_id
          from public.channels_accounts
         where type = (case p_source when 'meta' then 'cloud' else 'evolution' end)
           and external_account_id = p_source_account_id;

    else
        raise exception 'origem desconhecida: %', p_source;
    end if;

    if v_tenant_id is null then
        -- Conta que não conhecemos: nada é gravado e o chamador decide o que
        -- responder. Silenciar seria pior — o evento sumiria sem rastro.
        return row('unknown_account', null, null, null, null)::internal.ingest_outcome;
    end if;

    -- 2 · Idempotência. A chave inclui a conta de origem porque plataformas dão
    --     ids sequenciais POR LOJA: sem ela, o evento "1042" da segunda loja
    --     seria engolido como duplicata do "1042" da primeira.
    insert into internal.webhook_events
        (source, source_account_id, external_event_id, tenant_id, event_type, payload, status)
    values
        (p_source, p_source_account_id, p_external_event_id, v_tenant_id, p_event_type,
         p_payload, 'received')
    on conflict (source, source_account_id, external_event_id) do nothing
    returning id into v_event_id;

    if v_event_id is null then
        select id
          into v_event_id
          from internal.webhook_events
         where source = p_source
           and source_account_id = p_source_account_id
           and external_event_id = p_external_event_id;

        return row('duplicate', v_event_id, v_tenant_id, null, null)::internal.ingest_outcome;
    end if;

    -- 3a · Ramo de canal: a mensagem que chegou.
    if v_channel_account_id is not null and p_event_type = 'message_inbound' then
        v_phone := p_payload ->> 'from';
        if v_phone is null then
            raise exception 'mensagem sem remetente';
        end if;

        insert into public.contacts (tenant_id, phone_e164, last_message_at)
        values (v_tenant_id, v_phone, now())
        on conflict (tenant_id, phone_e164)
            do update set last_message_at = now()
        returning id into v_contact_id;

        select id
          into v_conversation_id
          from public.conversations
         where tenant_id = v_tenant_id
           and contact_id = v_contact_id
           and state <> 'encerrada'
         order by created_at desc
         limit 1;

        if v_conversation_id is null then
            insert into public.conversations
                (tenant_id, contact_id, channel_account_id, origin_occasion)
            values (v_tenant_id, v_contact_id, v_channel_account_id, 'direct')
            returning id into v_conversation_id;
        end if;

        v_seq := internal.next_message_seq(v_conversation_id, 'inbound');

        insert into public.messages
            (tenant_id, conversation_id, direction, seq, channel, author_type, content)
        values
            (v_tenant_id, v_conversation_id, 'inbound', v_seq,
             case p_source when 'meta' then 'whatsapp_cloud' else 'whatsapp_evolution' end,
             'contact', coalesce(p_payload -> 'message', '{}'::jsonb));

        -- O prazo do debounce: mensagem nova empurra. É só isso — a ingestão
        -- NÃO enfileira job de entrada. Quem cria job é o coalescer, numa
        -- transação só, com o contador de geração (ADR-7). Enfileirar aqui
        -- devolveria o duplo enfileiramento que a v1.2 eliminou.
        update public.conversations
           set pending_response_at = now() + p_debounce
         where id = v_conversation_id;

        update internal.webhook_events
           set status = 'processed', processed_at = now()
         where id = v_event_id;

        return row('ingested', v_event_id, v_tenant_id, v_conversation_id, v_seq)
            ::internal.ingest_outcome;
    end if;

    -- 3b · Ramo de plataforma: abandono, pagamento, status. Esse sim vira job.
    perform pgmq.send('q_domain_events', jsonb_build_object('webhook_event_id', v_event_id));

    update internal.webhook_events
       set status = 'enqueued'
     where id = v_event_id;

    return row('ingested', v_event_id, v_tenant_id, null, null)::internal.ingest_outcome;
end
$$;

-- ---------------------------------------------------------------------------
-- Quem pode chamar
-- ---------------------------------------------------------------------------
-- A função escreve atravessando tenants, então quem pode executá-la tem, na
-- prática, a chave da ingestão. EXECUTE mínimo e um papel próprio.
--
-- PENDÊNCIA REGISTRADA: qual identidade a Edge Function usa para chegar aqui
-- não está fixada em nenhum documento — ela não é `worker_role`. O papel existe
-- e a fiação real (login, pool, ou RPC pela Data API) é decisão da Fase 3,
-- quando a Edge Function for escrita. Enquanto isso ninguém além do dono
-- executa, que é o estado seguro.
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'ingestion_role') then
        create role ingestion_role nologin nobypassrls;
    end if;
end
$$;

grant ingestion_role to postgres;
grant usage on schema internal to ingestion_role;

revoke execute on function internal.ingest_webhook(text, text, text, text, jsonb, interval)
    from public;
grant execute on function internal.ingest_webhook(text, text, text, text, jsonb, interval)
    to ingestion_role;

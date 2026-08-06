-- E3 · S8 — reconciliação por poll: o cinto de segurança do ADR-3.
--
-- Um webhook é uma promessa de terceiro. A plataforma cai, a nossa Edge Function
-- responde 500 num deploy, a Meta reentrega e a Shopify não — e o pedido pago
-- que ninguém viu vira um funil perseguindo um cliente que já pagou. O poll
-- existe para que a perda de UMA entrega seja um atraso de quinze minutos em vez
-- de um erro permanente.
--
-- A decisão que molda este arquivo inteiro é a **D5**: a reconciliação chama a
-- MESMA `internal.ingest_webhook` que o webhook chama. Um segundo caminho de
-- escrita seria uma segunda idempotência para manter em dia — e a única forma de
-- descobrir que as duas divergiram seria um cliente recebendo a mesma mensagem
-- duas vezes. Então aqui não existe nenhum INSERT em `webhook_events`: existe um
-- cursor, uma reivindicação cross-tenant e um encerramento. O que entra, entra
-- pela porta que já estava lá.
--
-- Três coisas nascem juntas porque uma sem as outras mente:
--
--   * `connector_accounts.sync_cursor_at` — onde o último poll parou;
--   * `internal.claim_sync_targets` — as lojas vencidas, no molde de
--     `claim_due_touches`: cross-tenant, SECURITY DEFINER, sem parâmetro de
--     filtro (pedir "só o tenant X" seria consulta cross-tenant com outro nome);
--   * `internal.finish_sync` — o ÚNICO escritor de `sync_status`, `last_sync_at`
--     e do cursor, e o lugar onde "o cursor não regride" é uma expressão em vez
--     de uma promessa espalhada por quem chama.
--
-- E fecha a dívida nomeada do S5: `webhook_events` guardava `source` e
-- `source_account_id` como texto e cada handler refazia o join para
-- `connector_accounts` — dois lugares antes deste passo, três com o poll. A
-- ingestão já resolve essa linha para descobrir o tenant; agora ela GRAVA o id
-- que resolveu.

-- ---------------------------------------------------------------------------
-- O cursor, e o que `last_sync_at` passa a significar
-- ---------------------------------------------------------------------------
-- Timestamp e não page token: toda plataforma aqui expõe um filtro "atualizado
-- desde", e um instante é o único cursor cuja ordenação é definida sem
-- perguntar ao fornecedor. O limite inferior da busca é INCLUSIVO, então dois
-- eventos no mesmo segundo são relidos em vez de pulados — reler é o que a D5
-- torna gratuito.
alter table public.connector_accounts
    add column sync_cursor_at timestamptz;

comment on column public.connector_accounts.sync_cursor_at is
    'Instante do último evento efetivamente ingerido por poll. Só avança (internal.finish_sync). '
    'NULL = loja nunca reconciliada: o primeiro poll pega a janela inicial do adaptador.';

-- `last_sync_at` passa a ser "a última vez que PERGUNTAMOS", e `sync_status` diz
-- como aquela pergunta terminou. O par só significa alguma coisa junto, e é
-- essa leitura que dispensa uma coluna de claim: um passe que morreu no meio
-- deixa `syncing` com `last_sync_at` recente — visível como diagnóstico, e
-- naturalmente reagendado para o próximo intervalo em vez de martelado.
comment on column public.connector_accounts.last_sync_at is
    'Quando o último passe de reconciliação PERGUNTOU a esta loja (não "quando deu certo"). '
    'sync_status diz como terminou: ok | error | syncing (passe em andamento, ou morto).';

-- ---------------------------------------------------------------------------
-- A dívida do S5 — o join que cada handler refazia
-- ---------------------------------------------------------------------------
-- `on delete set null` e não `cascade`: desconectar uma loja não pode apagar o
-- rastro dos eventos que ela mandou. `source`/`source_account_id` continuam lá,
-- em texto, e continuam sendo a chave de idempotência — esta coluna é a
-- resolução, não a identidade.
alter table internal.webhook_events
    add column connector_account_id uuid references public.connector_accounts (id)
        on delete set null;

comment on column internal.webhook_events.connector_account_id is
    'A loja resolvida NA INGESTÃO (S8, dívida do S5). NULL para eventos de canal (meta/evolution) '
    'e para eventos ingeridos antes desta migration.';

-- ---------------------------------------------------------------------------
-- `internal.ingest_webhook` — a mesma porta, agora gravando o que já resolvia
-- ---------------------------------------------------------------------------
-- Corpo extraído de `20260802000005_ingestao.sql` (a definição VIVA e única —
-- nenhuma migration a substituiu desde então) e alterado em três linhas: uma
-- variável, um `id` no SELECT que já existia, e a coluna no INSERT. plpgsql não
-- tem substituição parcial, então uma função tocada é uma função reescrita
-- inteira, e reescrever de memória foi exatamente o que apagou em silêncio o
-- roteamento do S5 durante o S7.
--
-- Assinatura idêntica de propósito: a Edge Function chama esta função por nome
-- e aridade, e `create or replace` preserva os GRANTs.
create or replace function internal.ingest_webhook(
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
    v_tenant_id            uuid;
    v_channel_account_id   uuid;
    v_connector_account_id uuid;
    v_event_id             bigint;
    v_contact_id           uuid;
    v_conversation_id      uuid;
    v_seq                  integer;
    v_phone                text;
begin
    -- 1 · O tenant vem da CONTA DE ORIGEM. Nunca do payload — um `tenant_id`
    --     no corpo do webhook é dado de terceiro, e dado de terceiro não
    --     escolhe de quem é a conversa.
    if p_source in ('shopify', 'nuvemshop', 'yampi') then
        select id, tenant_id
          into v_connector_account_id, v_tenant_id
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
    --
    --     Esta é a linha que a D5 protege: o poll entra AQUI, com a mesma chave,
    --     e por isso replay três vezes pelos dois caminhos dá exatamente um
    --     efeito. Uma segunda porta de escrita seria uma segunda chave.
    insert into internal.webhook_events
        (source, source_account_id, external_event_id, tenant_id, event_type, payload, status,
         connector_account_id)
    values
        (p_source, p_source_account_id, p_external_event_id, v_tenant_id, p_event_type,
         p_payload, 'received', v_connector_account_id)
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
-- internal.claim_sync_targets — as lojas vencidas
-- ---------------------------------------------------------------------------
create type internal.sync_target as (
    connector_account_id uuid,
    tenant_id            uuid,
    platform             text,
    source_account_id    text,
    cursor_at            timestamptz
);

comment on type internal.sync_target is
    'Uma loja a reconciliar, como o poll a vê. cursor_at NULL = nunca reconciliada.';

-- No molde de `claim_due_touches` e de `claim_outbox_batch`: sem parâmetro de
-- filtro, `FOR UPDATE SKIP LOCKED`, marca o que pega e devolve só o que pegou.
--
-- `p_stale_after` é PARÂMETRO e não `interval '15 minutes'`: os quinze minutos
-- vivem na `QueueingConfig` do runtime, e um literal aqui seria a segunda cópia
-- de um número canônico — livre para divergir no dia em que alguém mudar a
-- primeira.
create function internal.claim_sync_targets(
    p_stale_after interval,
    p_limit       integer default 20
)
    returns setof internal.sync_target
    language sql
    security definer
    set search_path = pg_catalog, public, internal
as $$
    with due as (
        select id
          from public.connector_accounts
         -- `last_sync_at` é "quando perguntamos", então isto é literalmente
         -- "faz tempo demais que ninguém pergunta a esta loja" — incluindo a
         -- loja cujo passe anterior morreu em `syncing`, que é exatamente
         -- quem mais precisa de uma segunda pergunta.
         where last_sync_at is null
            or last_sync_at < now() - p_stale_after
         order by last_sync_at nulls first
         for update skip locked
         limit p_limit
    ),
    marked as (
        update public.connector_accounts a
           set sync_status  = 'syncing',
               last_sync_at = now()
          from due
         where a.id = due.id
        returning a.id, a.tenant_id, a.platform, a.source_account_id, a.sync_cursor_at
    )
    select id, tenant_id, platform, source_account_id, sync_cursor_at from marked
$$;

comment on function internal.claim_sync_targets(interval, integer) is
    'Varredura cross-tenant das lojas vencidas, no molde de claim_due_touches. Sem parâmetro '
    'de filtro: pedir "só o tenant X" seria consulta cross-tenant arbitrária (ADR-11).';

-- ---------------------------------------------------------------------------
-- internal.finish_sync — o único escritor, e onde o cursor não regride
-- ---------------------------------------------------------------------------
-- `greatest` ignora NULL, então três casos caem numa expressão só: primeira
-- sincronização (`sync_cursor_at` NULL → assume o novo), passe que não viu nada
-- (`p_cursor` NULL → mantém o que havia) e passe que falhou depois de ingerir
-- parte (mantém o que ingeriu, nunca o que pediu). É por isso que o cursor mora
-- aqui em vez de num UPDATE de quem chama: "não regride" precisa ser uma
-- propriedade da coluna, não uma disciplina de três call sites.
--
-- E é o que faz um passe que morre no meio não pular evento: o cursor só chega
-- ao instante do ÚLTIMO evento que a ingestão aceitou, então o próximo passe
-- recomeça de lá — relendo aquele (limite inferior inclusivo) e seguindo.
create function internal.finish_sync(
    p_connector_account_id uuid,
    p_status               text,
    p_cursor               timestamptz default null
)
    returns timestamptz
    language plpgsql
    security definer
    set search_path = pg_catalog, public, internal
as $$
declare
    v_cursor timestamptz;
begin
    if p_status not in ('ok', 'error') then
        -- `syncing` é estado de passe em andamento e quem o escreve é o claim.
        -- Encerrar em `syncing` seria encerrar sem encerrar.
        raise exception 'sync_status de encerramento inválido: %', p_status;
    end if;

    update public.connector_accounts
       set sync_status    = p_status,
           last_sync_at   = now(),
           sync_cursor_at = greatest(sync_cursor_at, p_cursor)
     where id = p_connector_account_id
    returning sync_cursor_at into v_cursor;

    if not found then
        raise exception 'conta de conector inexistente: %', p_connector_account_id;
    end if;

    return v_cursor;
end
$$;

comment on function internal.finish_sync(uuid, text, timestamptz) is
    'Encerra um passe de reconciliação: sync_status, last_sync_at e o cursor — que só avança. '
    'Devolve o cursor como ficou, para quem chamou poder ver que o dele foi recusado.';

-- ---------------------------------------------------------------------------
-- Quem pode chamar — e por que NÃO é o `worker_role`
-- ---------------------------------------------------------------------------
-- A varredura chama `internal.ingest_webhook`. Quem pode executá-la tem, na
-- prática, a chave da ingestão — e o teste do E1
-- (`test_the_worker_cannot_ingest_either`) diz, com essas palavras, que o
-- worker não é a ingestão e que o dia em que a reconciliação existisse o grant
-- seria "uma decisão consciente e não uma herança".
--
-- A decisão consciente é esta: a reconciliação NÃO vira privilégio do worker.
-- Ela roda numa conexão própria, com o papel que ela de fato é. Um `grant
-- ingestion_role to worker_role` teria sido mais curto e teria dado a chave da
-- ingestão a todo turno de LLM, a todo handler de evento e a todo toque
-- proativo — dois planos de execução fundidos por conveniência de fiação.
--
-- É por isso que os dois lados do passe vão para `ingestion_role` e não para
-- `worker_role`: o passe inteiro (reivindicar, ingerir, encerrar) pertence a um
-- papel só, e nenhuma parte dele empresta poder ao outro.
revoke execute on function internal.claim_sync_targets(interval, integer) from public;
revoke execute on function internal.finish_sync(uuid, text, timestamptz) from public;
grant execute on function internal.claim_sync_targets(interval, integer) to ingestion_role;
grant execute on function internal.finish_sync(uuid, text, timestamptz) to ingestion_role;

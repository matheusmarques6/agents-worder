-- E3 · S11 — o `traceparent` atravessa a porta da ingestão.
--
-- O `CLAUDE.md` diz, palavra por palavra: "`traceparent` viaja dentro dos
-- payloads de fila (coluna `otel`)". O slot existe desde o E1 e as quatro
-- classes de job o leem. `internal.ingest_webhook`, porém, é o produtor de TODO
-- job da `q_domain_events`, e ele montava o payload com uma chave só —
-- `webhook_event_id`. O contexto morria na porta.
--
-- Isso importa mais na reconciliação do que no webhook, e a razão é o próprio
-- desenho da D5: o poll e o webhook entram pela MESMA função. O webhook é uma
-- Edge Function, cujo trace começa fora do nosso processo; o passe de
-- reconciliação é uma tarefa do runtime, e o job que ele cria é consequência
-- direta de um tique nosso. Sem esta linha, "a loja X foi reconciliada" e "o
-- funil do pedido Y foi cancelado porque ele estava pago" são dois traces sem
-- relação nenhuma — e são a mesma história.
--
-- **Aditivo por assinatura, não por sorte.** `p_otel` entra como ÚLTIMO
-- parâmetro, com default: a Edge Function chama a função por nome e aridade
-- (`supabase/functions/ingest-meta/index.ts` passa cinco argumentos), e um
-- parâmetro novo no meio teria trocado o significado posicional dos que já
-- existiam. O runtime N-1 continua válido contra este schema: toda chamada que
-- ele sabe fazer continua resolvendo, agora com `p_otel` nulo.
--
-- **DROP e recreate, e não `create or replace`.** Um parâmetro a mais é uma
-- assinatura diferente, e `create or replace` teria criado uma SEGUNDA função
-- em vez de substituir a primeira — deixando a chamada de cinco argumentos da
-- Edge Function ambígua entre as duas e quebrando a ingestão em produção com um
-- "function is not unique". É o mesmo movimento do S7 com `claim_outbox_batch`,
-- inclusive na parte que se esquece: os GRANTs vão junto, reafirmados abaixo,
-- porque o DROP os leva.
--
-- E o corpo abaixo foi COPIADO da definição viva (`20260806000010`), não
-- lembrado: plpgsql não tem substituição parcial, então uma função tocada é uma
-- função reescrita inteira — e reescrever de memória foi exatamente o que apagou
-- em silêncio o roteamento do S5 durante o S7. As mudanças são duas: o parâmetro
-- e a chave no `pgmq.send`.
--
-- O que NÃO está aqui: exportador, SDK e endpoint OTLP. Dependem do Logfire e do
-- Grafana Cloud (pendências B-2/B-3). Isto é só o carregamento do contexto — a
-- metade que existe sem credencial, e a metade que, faltando, torna a outra
-- inútil no dia em que chegar.

drop function internal.ingest_webhook(text, text, text, text, jsonb, interval);

create function internal.ingest_webhook(
    p_source            text,
    p_source_account_id text,
    p_external_event_id text,
    p_event_type        text,
    p_payload           jsonb,
    -- O debounce canônico é 10s (CLAUDE.md). Parâmetro com default para a suíte
    -- de pipeline rodar com valores minúsculos em vez de esperar de verdade.
    p_debounce          interval default interval '10 seconds',
    -- O contexto W3C de quem bateu na porta, no formato que todo propagador do
    -- OpenTelemetry injeta e extrai: {"traceparent": "...", "tracestate": "..."}.
    -- NULL é a resposta honesta de quem não tem contexto — e não `'{}'`, que
    -- diria "houve um trace e ele estava vazio".
    p_otel              jsonb default null
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
        --
        -- (E é por isso que `p_otel` não tem o que fazer neste ramo: o job que
        -- vai nascer é do coalescer, dois segundos depois, e o contexto dele é
        -- o do tique do coalescer — não o desta chamada.)
        update public.conversations
           set pending_response_at = now() + p_debounce
         where id = v_conversation_id;

        update internal.webhook_events
           set status = 'processed', processed_at = now()
         where id = v_event_id;

        return row('ingested', v_event_id, v_tenant_id, v_conversation_id, v_seq)
            ::internal.ingest_outcome;
    end if;

    -- 3b · Ramo de plataforma: abandono, pagamento, status. Esse sim vira job —
    -- e o job carrega o contexto de quem bateu na porta. `p_otel` NULL não vira
    -- chave nenhuma: `jsonb_strip_nulls` tira o slot vazio em vez de deixar um
    -- `"otel": null` que não afirma nada em todo job da fila.
    perform pgmq.send(
        'q_domain_events',
        jsonb_strip_nulls(jsonb_build_object(
            'webhook_event_id', v_event_id,
            'otel', p_otel
        ))
    );

    update internal.webhook_events
       set status = 'enqueued'
     where id = v_event_id;

    return row('ingested', v_event_id, v_tenant_id, null, null)::internal.ingest_outcome;
end
$$;

comment on function internal.ingest_webhook(text, text, text, text, jsonb, interval, jsonb) is
    'A porta única da ingestão (D5: o poll entra por aqui também). p_otel é o contexto W3C de '
    'quem bateu, e ele viaja para dentro do job de q_domain_events (CLAUDE.md, observabilidade).';

-- Os GRANTs que o DROP levou, reafirmados na mesma transação — a ingestão nunca
-- fica sem porta. `ingestion_role` e mais ninguém: quem pode executar isto tem,
-- na prática, a chave de escrever em qualquer tenant, e o E1 já registrou que o
-- worker não a recebe.
revoke execute on function
    internal.ingest_webhook(text, text, text, text, jsonb, interval, jsonb) from public;
grant execute on function
    internal.ingest_webhook(text, text, text, text, jsonb, interval, jsonb) to ingestion_role;

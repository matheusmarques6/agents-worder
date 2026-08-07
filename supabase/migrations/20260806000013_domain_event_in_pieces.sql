-- E3 · S10 — `internal.apply_domain_event` quebrada em peças pequenas.
--
-- **A cicatriz que motiva esta migration está escrita na 0009, em voz alta:**
-- "a primeira versão deste arquivo transcreveu o corpo da migration 0003 em vez
-- do da 0005, e com isso apagou o roteamento de `order_paid` que o S5 tinha
-- acrescentado". Dois cenários de `pipeline` reprovaram na hora — desta vez.
--
-- A causa não foi desatenção, foi FORMA. plpgsql não tem substituição parcial:
-- mudar uma linha de uma função exige reescrever o corpo inteiro, e um corpo
-- inteiro é a unidade errada para uma revisão. Quanto maior ele fica, mais
-- barata fica a transcrição errada e mais cara fica a leitura que a pegaria. A
-- 0009 tinha 130 linhas e quatro responsabilidades; a próxima teria mais.
--
-- Então a contração é esta: o corpo vira ROTEAMENTO, e cada coisa que ele fazia
-- vira uma função com nome. O ganho não é estético, é mecânico —
-- `create or replace` passa a poder atingir UMA das peças, e a peça que ninguém
-- tocou não pode ser apagada por quem estava mexendo em outra. É a mesma razão
-- pela qual o dispatcher tem três fases nomeadas em vez de um bloco.
--
-- **O corpo abaixo foi EXTRAÍDO da 0009, não redigitado** — a mesma regra que a
-- 0009 deixou escrita para quem viesse depois. O que mudou foi só onde cada
-- trecho passou a morar; nenhuma condição, nenhuma ordem de desfecho e nenhum
-- valor de retorno mudou. A ordem continua sendo a carregada de significado:
-- `already_applied` antes de tudo, `order_paid` para o seu próprio handler,
-- tipo não suportado, payload, saída, e só então o funil — com `no_funnel`
-- DEPOIS de `no_channel`, porque uma conexão quebrada é falha nossa e um funil
-- desligado é escolha do lojista.
--
-- As peças são SECURITY INVOKER de propósito. Chamadas de dentro da função
-- DEFINER elas rodam como o dono, que é o que sempre aconteceu; chamadas
-- diretamente por um papel da aplicação elas rodam como esse papel, com RLS no
-- caminho. Uma peça extraída não pode ser uma porta nova para o privilégio da
-- função de onde ela saiu.

-- ---------------------------------------------------------------------------
-- 1. O rastro do evento — as cinco atualizações que eram a mesma
-- ---------------------------------------------------------------------------
-- Cinco cópias de `update internal.webhook_events set status = ..., processed_at
-- = now()` viviam no corpo, e uma delas esquecer o `processed_at` seria um
-- evento eternamente "em aberto" que nenhum teste olha diretamente. Uma cópia
-- só, e o status vira o argumento.
create function internal.close_webhook_event(
    p_webhook_event_id bigint,
    p_status           text
)
    returns void
    language sql
    set search_path = pg_catalog, public, internal
as $$
    update internal.webhook_events
       set status = p_status, processed_at = now()
     where id = p_webhook_event_id;
$$;

comment on function internal.close_webhook_event(bigint, text) is
    'S10: o rastro do evento em um lugar só. processed_at anda junto do status, sempre.';

-- ---------------------------------------------------------------------------
-- 2. O telefone que a plataforma mandou
-- ---------------------------------------------------------------------------
-- E.164 com `+`, o contrato do payload de plataforma. IMMUTABLE e sem
-- dependência de sessão: é um predicado sobre texto, e ele existir com nome é o
-- que impede que a expressão regular seja recopiada na próxima função que
-- precisar dela — o mesmo raciocínio das janelas da escada, que viajam como
-- parâmetro em vez de serem reescritas em SQL.
create function internal.is_e164(p_phone text)
    returns boolean
    language sql
    immutable
    set search_path = pg_catalog
as $$
    select p_phone is not null and p_phone ~ '^\+[1-9][0-9]{7,14}$';
$$;

comment on function internal.is_e164(text) is
    'RNF/dicionário: telefone em E.164 com +. Uma cópia da regra, não uma por chamador.';

-- ---------------------------------------------------------------------------
-- 3. O espelho do pedido que veio junto do evento
-- ---------------------------------------------------------------------------
-- Extraído palavra por palavra da 0009. Espelhado ANTES da busca do funil, e
-- deliberadamente mesmo quando o desfecho acaba sendo `no_funnel`: um pedido é
-- fato do lojista e o espelho responde à RF-070, não a haver funil configurado.
-- Isso é diferente da pessoa e da conversa vazia que `start_funnel_run` se
-- recusa a deixar para trás.
--
-- Devolve o id do pedido espelhado, ou NULL quando o evento não trouxe pedido,
-- quando o payload não tem a forma de um, ou quando a loja de origem não é uma
-- que conhecemos.
create function internal.mirror_event_order(
    p_event internal.webhook_events,
    p_phone text
)
    returns uuid
    language plpgsql
    set search_path = pg_catalog, public, internal
as $$
declare
    v_order      jsonb;
    v_connector  uuid;
    v_contact_id uuid;
begin
    v_order := p_event.payload -> 'order';

    if v_order is null
       or jsonb_typeof(v_order) <> 'object'
       or coalesce(v_order ->> 'external_id', '') = ''
    then
        return null;
    end if;

    select id
      into v_connector
      from public.connector_accounts
     where tenant_id = p_event.tenant_id
       and platform = p_event.source
       and source_account_id = p_event.source_account_id;

    if v_connector is null then
        return null;
    end if;

    select id
      into v_contact_id
      from public.contacts
     where tenant_id = p_event.tenant_id
       and phone_e164 = p_phone;

    -- NULL status: an abandonment makes no claim about payment, and saying
    -- `pending` here would push a paid order backwards.
    return internal.mirror_order(
        p_event.tenant_id, v_connector, v_order, v_contact_id,
        null, p_event.received_at);
end
$$;

comment on function internal.mirror_event_order(internal.webhook_events, text) is
    'S10: o bloco de espelho do pedido do abandono, extraído inteiro da 0009. '
    'NULL quando não há pedido, quando a forma não é a de um, ou quando a loja é desconhecida.';

-- ---------------------------------------------------------------------------
-- 4. O elo que fecha depois que o contato passa a existir
-- ---------------------------------------------------------------------------
-- `start_funnel_run` faz upsert do contato, então o pedido que foi espelhado
-- antes dele pode agora ganhar dono. Cobre o único caso que o espelho não
-- conseguia — primeiro abandono de um contato que nunca vimos — e NUNCA
-- sobrescreve um dono existente, para que um pedido re-sincronizado mantenha a
-- sua pessoa.
create function internal.link_order_to_contact(
    p_order_id  uuid,
    p_tenant_id uuid,
    p_phone     text
)
    returns void
    language sql
    set search_path = pg_catalog, public, internal
as $$
    update public.orders o
       set contact_id = c.id
      from public.contacts c
     where o.id = p_order_id
       and c.tenant_id = p_tenant_id
       and c.phone_e164 = p_phone
       and o.contact_id is null;
$$;

comment on function internal.link_order_to_contact(uuid, uuid, text) is
    'S10: fecha o elo pedido→contato depois do upsert de start_funnel_run. Nunca sobrescreve.';

-- ---------------------------------------------------------------------------
-- 5. `apply_domain_event`, agora só o roteamento
-- ---------------------------------------------------------------------------
-- O que sobrou é a ORDEM dos desfechos e nada mais — que é exatamente a parte
-- que carrega significado e a parte que uma revisão consegue ler inteira. Se
-- alguém precisar mudar como um pedido é espelhado, o `create or replace`
-- atinge `mirror_event_order` e este corpo não é sequer aberto.
create or replace function internal.apply_domain_event(p_webhook_event_id bigint)
    returns internal.domain_event_outcome
    language plpgsql
    security definer
    set search_path = pg_catalog, public, internal
as $$
declare
    v_event      internal.webhook_events%rowtype;
    v_phone      text;
    v_channel_id uuid;
    v_order_id   uuid;
    v_run        internal.funnel_run_outcome;
begin
    select * into v_event
      from internal.webhook_events
     where id = p_webhook_event_id;

    if not found then
        raise exception 'domain event % does not exist', p_webhook_event_id;
    end if;

    -- Redelivery guard, first as always. pgmq redelivers for every normal
    -- reason; the second application must be a non-event — one abandoned cart,
    -- one funnel; one payment, one cancellation, one conversion.
    if v_event.status = 'processed' then
        return row('already_applied', null, null)::internal.domain_event_outcome;
    end if;

    -- The payment has its own handler since S5.
    if v_event.event_type = 'order_paid' then
        return internal.apply_order_paid(p_webhook_event_id);
    end if;

    -- Only the occasions a funnel can have. Anything else is discarded with a
    -- trail, never failed — reprocessing an unsupported type would not change
    -- the outcome.
    if v_event.event_type not in ('checkout_abandoned', 'cart_abandoned', 'pix_pending') then
        perform internal.close_webhook_event(p_webhook_event_id, 'discarded');
        return row('discarded', null, null)::internal.domain_event_outcome;
    end if;

    v_phone := v_event.payload ->> 'phone';
    if not internal.is_e164(v_phone) then
        perform internal.close_webhook_event(p_webhook_event_id, 'failed');
        return row('invalid_payload', null, null)::internal.domain_event_outcome;
    end if;

    -- A saída é a que a configuração do funil pede (S7). `no_channel` cobre
    -- tanto "não há número ativo" quanto "o lojista pediu Evolution e não há
    -- número Evolution ativo" — as duas são conexão faltando.
    v_channel_id := internal.route_channel_account(v_event.tenant_id, v_event.event_type);

    if v_channel_id is null then
        perform internal.close_webhook_event(p_webhook_event_id, 'failed');
        return row('no_channel', null, null)::internal.domain_event_outcome;
    end if;

    v_order_id := internal.mirror_event_order(v_event, v_phone);

    -- `received_at`, never `now()`: the instant the platform's fact reached us
    -- is what staleness compares a newer message against.
    select * into v_run
      from internal.start_funnel_run(
               v_event.tenant_id,
               v_event.event_type,
               v_phone,
               v_channel_id,
               v_event.received_at,
               v_order_id);

    if v_run.status = 'no_funnel' then
        perform internal.close_webhook_event(p_webhook_event_id, 'discarded');
        return row('no_funnel', null, null)::internal.domain_event_outcome;
    end if;

    if v_order_id is not null then
        perform internal.link_order_to_contact(v_order_id, v_event.tenant_id, v_phone);
    end if;

    perform internal.close_webhook_event(p_webhook_event_id, 'processed');

    return row('applied', v_run.conversation_id, null)::internal.domain_event_outcome;
end
$$;

-- ---------------------------------------------------------------------------
-- Quem pode chamar as peças
-- ---------------------------------------------------------------------------
-- EXECUTE mínimo (ADR-11) mesmo sendo INVOKER: o privilégio de uma peça não é
-- o argumento — o argumento é que uma superfície pública que ninguém precisa é
-- uma superfície que alguém acaba usando. `is_e164` fica de fora da revogação
-- por ser um predicado puro sobre um texto que o chamador já tem na mão, sem
-- acesso a tabela nenhuma.
revoke execute on function internal.close_webhook_event(bigint, text) from public;
revoke execute on function internal.mirror_event_order(internal.webhook_events, text) from public;
revoke execute on function internal.link_order_to_contact(uuid, uuid, text) from public;

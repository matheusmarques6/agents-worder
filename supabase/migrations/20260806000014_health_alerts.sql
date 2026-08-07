-- E3 · S11 — as falhas do marco que ninguém abria.
--
-- O marco nasceu com UM alerta: a travessia dos 80% do tier do Meta, escrita no
-- S7 dentro de `internal.record_channel_send` porque o RF-035 pede a pausa **e**
-- o aviso. As outras três falhas deste marco eram silenciosas, e cada uma delas
-- é um jeito de o produto parar de falar sem que nada diga:
--
--   1. **toque preso em `enqueued`** (achado do S4). `claim_due_touches` marca o
--      toque e enfileira o job na mesma transação. Se o job morre na DLQ, o
--      toque fica `enqueued` para sempre: a varredura só pega `pending`, e
--      aquela mensagem não vai sair nunca mais. A decisão registrada no plano é
--      **alerta de IDADE, jamais um segundo relógio** — um relógio que
--      devolvesse o toque para `pending` poderia reenviar o que já saiu, que é
--      exatamente a duplicidade que o CAS do S4 existe para impedir. Por isso
--      esta migration não tem um único UPDATE em `scheduled_touches`;
--
--   2. **número banido** (`channels_accounts.status = 'banned'`). Nada no
--      caminho de envio lê esse estado: `claim_outbox_batch` entrega a linha e
--      `dispatch_touch` escreve na outbox de um número que não fala;
--
--   3. **loja em erro persistente**. O S8 fecha a varredura em `sync_status =
--      'error'` e ninguém olha. "Persistente" é a palavra difícil aqui: não dá
--      para medi-la com `last_sync_at`, porque `finish_sync` o move a CADA
--      passe — uma loja que falha de cinco em cinco minutos tem `last_sync_at`
--      eternamente fresco. Daí a coluna nova abaixo.
--
-- **PII nunca**, e a lei vale para alerta como vale para telemetria
-- (`CLAUDE.md`): um alerta é lido fora do Postgres. Ids, contadores e idades;
-- nunca telefone, nome ou conteúdo de mensagem.
--
-- **A deduplicação é parte do desenho, não otimização.** Uma varredura que
-- abrisse uma linha por tique produziria, em um dia ruim, mais alertas do que
-- qualquer pessoa lê — e um alerta que ninguém lê é a mesma doença que este
-- passo está curando, com outro sintoma. Enquanto existir alerta ABERTO
-- equivalente, nada é aberto; resolvido sem conserto, o próximo tique avisa de
-- novo.

-- ---------------------------------------------------------------------------
-- 1. Desde quando esta loja falha — e o escritor nasce junto
-- ---------------------------------------------------------------------------
-- `last_sync_at` responde "quando perguntamos" e `sync_status` responde "como
-- terminou". Falta o terceiro fato, que é o único que distingue um soluço de
-- uma loja quebrada: HÁ QUANTO TEMPO ela vem terminando mal.
alter table public.connector_accounts
    add column sync_error_since timestamptz;

comment on column public.connector_accounts.sync_error_since is
    'Início da sequência ININTERRUPTA de passes com sync_status = error. NULL = a última '
    'reconciliação terminou bem. Escrito por internal.finish_sync(); é o que mede persistência, '
    'porque last_sync_at avança a cada passe inclusive nos que falham.';

-- `create or replace` com a MESMA assinatura: o corpo abaixo é o de
-- `20260806000010_reconciliation.sql` com três linhas a mais no UPDATE. plpgsql
-- não tem substituição parcial, então uma função tocada é uma função reescrita
-- inteira — e reescrever de memória foi o que apagou em silêncio o roteamento
-- do S5 durante o S7. Este corpo foi copiado da definição viva, não lembrado.
create or replace function internal.finish_sync(
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
           sync_cursor_at = greatest(sync_cursor_at, p_cursor),
           -- DESDE QUANDO, não "a última vez". Um erro que se repete mantém o
           -- carimbo do primeiro: movê-lo a cada falha faria a loja que falha
           -- sem parar parecer eternamente recente, que é o defeito de
           -- `last_sync_at` que esta coluna existe para corrigir. Um passe que
           -- termina bem apaga o carimbo, e a sequência recomeça do zero.
           sync_error_since = case
               when p_status = 'ok' then null
               else coalesce(sync_error_since, now())
           end
     where id = p_connector_account_id
    returning sync_cursor_at into v_cursor;

    if not found then
        raise exception 'conta de conector inexistente: %', p_connector_account_id;
    end if;

    return v_cursor;
end
$$;

comment on function internal.finish_sync(uuid, text, timestamptz) is
    'Encerra um passe de reconciliação: sync_status, last_sync_at, sync_error_since e o cursor — '
    'que só avança. Devolve o cursor como ficou, para quem chamou poder ver que o dele foi recusado.';

-- ---------------------------------------------------------------------------
-- 2. Dois tipos novos de alerta — e a expansão que aceita mais do que antes
-- ---------------------------------------------------------------------------
-- Expand-contract: alargar um CHECK aceita estritamente mais do que aceitava,
-- então o runtime N-1 continua válido contra este schema — todo valor que ele
-- sabe escrever continua sendo aceito. É o mesmo movimento do S9 (`handoff`).
--
-- `connector_error` NÃO entra aqui: ele já existia no CHECK desde o E2, sem
-- ninguém que o escrevesse. Este é o commit que lhe dá escritor.
alter table public.alerts drop constraint alerts_type_check;

alter table public.alerts add constraint alerts_type_check
    check (type in ('critical_violation', 'queue_depth', 'queue_age', 'dlq',
                    'outbox_unknown', 'outbox_review', 'meta_tier',
                    'connector_error', 'lease_expired', 'handoff',
                    -- Toque reivindicado que nunca virou nada: o job morreu e
                    -- `claim_due_touches` não olha para `enqueued`.
                    'touch_stuck',
                    -- O número não fala mais. Não é envio atrasado: é a loja
                    -- inteira muda naquele canal.
                    'channel_banned'));

comment on constraint alerts_type_check on public.alerts is
    'E3 S11 acrescentou `touch_stuck` e `channel_banned`; `connector_error` existia desde o E2 e '
    'ganhou escritor no mesmo passo.';

-- ---------------------------------------------------------------------------
-- 3. A varredura de saúde
-- ---------------------------------------------------------------------------
-- SECURITY DEFINER e sem parâmetro de filtro, no molde de `claim_due_touches`,
-- `claim_outbox_batch` e `claim_sync_targets`: a varredura é cross-tenant por
-- natureza e nenhum papel da aplicação pode ter SELECT global (ADR-11). Um
-- parâmetro "só o tenant X" seria consulta cross-tenant arbitrária com outro
-- nome.
--
-- Os dois prazos chegam como PARÂMETRO, pela disciplina que o `dispatch_touch`
-- do S4 e o `claim_sync_targets` do S8 já seguem: eles moram na
-- `QueueingConfig` do runtime, e um `interval '30 minutes'` aqui seria a segunda
-- cópia de um número — livre para divergir no dia em que alguém mudar a
-- primeira.
--
-- Devolve QUANTOS alertas abriu, e não quantas situações encontrou: é o número
-- que um teste consegue afirmar e o que um operador quer saber depois de um
-- tique ("aconteceu alguma coisa nova?").
create function internal.sweep_health_alerts(
    p_touch_stuck_after interval,
    p_sync_error_after  interval
)
    returns integer
    language plpgsql
    security definer
    set search_path = pg_catalog, public, internal
as $$
declare
    v_opened integer := 0;
    v_count  integer;
begin
    -- 3.1 · Toques presos em `enqueued` (achado do S4) --------------------------
    -- UM alerta por tenant, com a contagem e o mais velho — não um por toque.
    -- No dia em que a DLQ enche, um alerta por linha é uma tempestade que
    -- ninguém lê; a lista completa é da view de contadores, que existe para
    -- isso. `coalesce(claimed_at, created_at)` porque um `enqueued` sem
    -- `claimed_at` seria um defeito do claim, e a leitura errada seria
    -- ESCONDÊ-LO da varredura.
    with stuck as (
        select tenant_id,
               count(*)                                                   as stuck_count,
               min(coalesce(claimed_at, created_at))                      as oldest_at,
               (array_agg(id order by coalesce(claimed_at, created_at)))[1] as oldest_id
          from public.scheduled_touches
         where status = 'enqueued'
           and coalesce(claimed_at, created_at) < now() - p_touch_stuck_after
         group by tenant_id
    ),
    inserted as (
        insert into public.alerts (tenant_id, type, severity, title, payload)
        select s.tenant_id,
               'touch_stuck',
               'warning',
               'Toques proativos parados: ' || s.stuck_count
                   || ' sem sair há mais de ' || p_touch_stuck_after,
               -- Sem PII: nem contato, nem telefone, nem a copy do toque. O id
               -- do toque e a idade são o que um humano precisa para achar a
               -- linha na DLQ.
               jsonb_build_object(
                   'stuck_count', s.stuck_count,
                   'oldest_scheduled_touch_id', s.oldest_id,
                   'oldest_age_seconds', floor(extract(epoch from now() - s.oldest_at))::bigint)
          from stuck s
         where not exists (
             select 1
               from public.alerts a
              where a.tenant_id = s.tenant_id
                and a.type = 'touch_stuck'
                and a.status = 'open')
        returning 1
    )
    select count(*) into v_count from inserted;
    v_opened := v_opened + v_count;

    -- 3.2 · Número banido -------------------------------------------------------
    -- Um por conta de canal: são poucas por lojista, e qual delas morreu é a
    -- única informação acionável.
    with inserted as (
        insert into public.alerts (tenant_id, type, severity, title, payload)
        select ca.tenant_id,
               'channel_banned',
               'critical',
               'Número do WhatsApp banido — nada entra nem sai por ele',
               jsonb_build_object(
                   'channel_account_id', ca.id,
                   'channel_type', ca.type)
          from public.channels_accounts ca
         where ca.status = 'banned'
           and not exists (
               select 1
                 from public.alerts a
                where a.type = 'channel_banned'
                  and a.status = 'open'
                  and a.payload ->> 'channel_account_id' = ca.id::text)
        returning 1
    )
    select count(*) into v_count from inserted;
    v_opened := v_opened + v_count;

    -- 3.3 · Loja em erro persistente -------------------------------------------
    -- `sync_error_since` e não `last_sync_at`: ver a coluna acima. Uma
    -- plataforma que oscila por dez minutos se resolve sozinha — o cinto de
    -- segurança do ADR-3 é feito de repetição —, então alertar no primeiro erro
    -- seria alertar sobre o mecanismo funcionando.
    with inserted as (
        insert into public.alerts (tenant_id, type, severity, title, payload)
        select ca.tenant_id,
               'connector_error',
               'warning',
               'Loja sem reconciliar: os passes vêm falhando há mais de ' || p_sync_error_after,
               jsonb_build_object(
                   'connector_account_id', ca.id,
                   'platform', ca.platform,
                   'error_for_seconds',
                   floor(extract(epoch from now() - ca.sync_error_since))::bigint)
          from public.connector_accounts ca
         where ca.sync_status = 'error'
           and ca.sync_error_since is not null
           and ca.sync_error_since < now() - p_sync_error_after
           and not exists (
               select 1
                 from public.alerts a
                where a.type = 'connector_error'
                  and a.status = 'open'
                  and a.payload ->> 'connector_account_id' = ca.id::text)
        returning 1
    )
    select count(*) into v_count from inserted;
    v_opened := v_opened + v_count;

    return v_opened;
end
$$;

comment on function internal.sweep_health_alerts(interval, interval) is
    'As três falhas silenciosas do E3 viram linha em public.alerts: toque preso em enqueued, '
    'número banido, loja em erro persistente. Observa e conta — não conserta nada, e em '
    'particular NUNCA devolve um toque para pending (isso seria um segundo relógio, e reenvio).';

-- ---------------------------------------------------------------------------
-- Quem executa
-- ---------------------------------------------------------------------------
-- Tarefa periódica do processo, ao lado do coalescer e da varredura de silêncio:
-- roda com o papel do worker. O `sender_role` não a recebe pelo mesmo motivo que
-- o `worker_role` não recebe `claim_sync_targets` — cada papel só ganha a porta
-- que o trabalho dele de fato usa.
revoke execute on function internal.sweep_health_alerts(interval, interval) from public;
grant execute on function internal.sweep_health_alerts(interval, interval) to worker_role;

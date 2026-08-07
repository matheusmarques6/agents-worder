-- E3 · S7 — a preferência de canal ganha leitor, e a violação de copy ganha voz.
--
-- Duas coisas pequenas e uma doutrina.
--
-- **`funnels.channel_preference` existe desde o S2 e ninguém a lê.** O
-- roteamento escolhia "a conta ativa mais nova" e o próprio comentário do S3
-- dizia, em voz alta, "quando existirem regras de roteamento (S7) elas moram na
-- config". Config sem consumidor mente: um lojista que escolhe `evolution` na
-- tela do E5 e vê os toques saindo pela Cloud não tem como saber que a escolha
-- dele nunca foi lida. É o mesmo achado do S4 sobre `start_funnel_run`, e a
-- regra do marco: guarda sem alvo mente.
--
-- **Preferência explícita é ESTRITA.** `cloud` e `evolution` não caem para o
-- outro canal quando não há conta do tipo pedido: caem em `no_channel`, que já
-- significa exatamente isto — "falta uma conexão que um humano precisa
-- arrumar". Um fallback silencioso faria o produto ignorar a configuração
-- justamente no caso em que ela foi escrita de propósito. `auto` continua sendo
-- o que era: a conta ativa mais nova, qualquer que seja o tipo.

-- ---------------------------------------------------------------------------
-- 1. Quem escolhe a saída
-- ---------------------------------------------------------------------------
create function internal.route_channel_account(
    p_tenant_id uuid,
    p_occasion  text
)
    returns uuid
    language sql
    stable
    security definer
    set search_path = pg_catalog, public, internal
as $$
    -- A preferência do funil desta ocasião, se houver funil habilitado. Sem
    -- funil, `auto`: a decisão de qual canal usar não pode depender de uma
    -- linha que talvez não exista, ou a ordem dos desfechos mudaria — o S3
    -- fixou que `no_channel` vem ANTES de `no_funnel`, porque uma conexão
    -- quebrada é falha nossa e um funil desligado é escolha do lojista.
    with preference as (
        select coalesce(
                   (select f.channel_preference
                      from public.funnels f
                     where f.tenant_id = p_tenant_id
                       and f.occasion = p_occasion
                       and f.enabled),
                   'auto') as value
    )
    select ca.id
      from public.channels_accounts ca, preference p
     where ca.tenant_id = p_tenant_id
       and ca.status = 'active'
       and (p.value = 'auto' or ca.type = p.value)
     order by ca.created_at desc
     limit 1;
$$;

comment on function internal.route_channel_account(uuid, text) is
    'S7: funnels.channel_preference finalmente tem leitor. cloud|evolution são estritos — '
    'sem conta do tipo pedido o desfecho é no_channel, nunca o outro canal em silêncio.';

revoke execute on function internal.route_channel_account(uuid, text) from public;

-- ---------------------------------------------------------------------------
-- 2. `apply_domain_event` passa a perguntar
-- ---------------------------------------------------------------------------
-- Substituição integral porque plpgsql não tem meio-termo. O corpo abaixo é o da
-- migration 20260806000005 palavra por palavra — foi EXTRAÍDO dela, não
-- redigitado — com um único bloco trocado, marcado no lugar.
--
-- Isto é uma cicatriz, e ela fica registrada: a primeira versão deste arquivo
-- transcreveu o corpo da migration 0003 em vez do da 0005, e com isso apagou o
-- roteamento de `order_paid` que o S5 tinha acrescentado. Dois cenários de
-- `pipeline` reprovaram na hora — que é exatamente o que aqueles testes existem
-- para fazer, e a razão pela qual a suíte inteira roda antes de cada commit.
-- Quem mexer aqui de novo: extraia o corpo vigente, não confie na memória.

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
    v_order      jsonb;
    v_connector  uuid;
    v_contact_id uuid;
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
        update internal.webhook_events
           set status = 'discarded', processed_at = now()
         where id = p_webhook_event_id;
        return row('discarded', null, null)::internal.domain_event_outcome;
    end if;

    v_phone := v_event.payload ->> 'phone';
    if v_phone is null or v_phone !~ '^\+[1-9][0-9]{7,14}$' then
        update internal.webhook_events
           set status = 'failed', processed_at = now()
         where id = p_webhook_event_id;
        return row('invalid_payload', null, null)::internal.domain_event_outcome;
    end if;

    -- >>> O ÚNICO BLOCO QUE MUDOU NO S7 <<<
    -- A saída deixa de ser "a conta ativa mais nova" e passa a ser a que a
    -- configuração do funil pede. `no_channel` continua significando o que
    -- significava, e agora também cobre "o lojista pediu Evolution e não há
    -- número Evolution ativo" — que é, de fato, uma conexão faltando.
    v_channel_id := internal.route_channel_account(v_event.tenant_id, v_event.event_type);

    if v_channel_id is null then
        update internal.webhook_events
           set status = 'failed', processed_at = now()
         where id = p_webhook_event_id;
        return row('no_channel', null, null)::internal.domain_event_outcome;
    end if;

    -- The order this funnel is chasing, when the event carries one. Absent — as
    -- it is for every platform that does not send the order with the
    -- abandonment yet — nothing is mirrored and the cadence is exactly what S3
    -- produced. Additive, both in the schema and in the behaviour.
    --
    -- Mirrored BEFORE the funnel lookup, and deliberately even when the outcome
    -- turns out to be `no_funnel`: an order is the merchant's own fact and the
    -- mirror is answerable to RF-070, not to whether a funnel was configured.
    -- That is a different thing from the person and the empty conversation
    -- `start_funnel_run` refuses to leave behind.
    v_order := v_event.payload -> 'order';
    if v_order is not null
       and jsonb_typeof(v_order) = 'object'
       and coalesce(v_order ->> 'external_id', '') <> ''
    then
        select id
          into v_connector
          from public.connector_accounts
         where tenant_id = v_event.tenant_id
           and platform = v_event.source
           and source_account_id = v_event.source_account_id;

        if v_connector is not null then
            select id
              into v_contact_id
              from public.contacts
             where tenant_id = v_event.tenant_id
               and phone_e164 = v_phone;

            -- NULL status: an abandonment makes no claim about payment, and
            -- saying `pending` here would push a paid order backwards.
            v_order_id := internal.mirror_order(
                v_event.tenant_id, v_connector, v_order, v_contact_id,
                null, v_event.received_at);
        end if;
    end if;

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
        update internal.webhook_events
           set status = 'discarded', processed_at = now()
         where id = p_webhook_event_id;
        return row('no_funnel', null, null)::internal.domain_event_outcome;
    end if;

    -- The contact exists now, whether or not it did when the order was
    -- mirrored: `start_funnel_run` upserts it. This closes the link in the one
    -- order the mirror could not — first abandonment of a contact we had never
    -- seen — and it never overwrites one, so a re-synced order keeps its person.
    if v_order_id is not null then
        update public.orders o
           set contact_id = c.id
          from public.contacts c
         where o.id = v_order_id
           and c.tenant_id = v_event.tenant_id
           and c.phone_e164 = v_phone
           and o.contact_id is null;
    end if;

    update internal.webhook_events
       set status = 'processed', processed_at = now()
     where id = p_webhook_event_id;

    return row('applied', v_run.conversation_id, null)::internal.domain_event_outcome;
end
$$;

-- ---------------------------------------------------------------------------
-- 3. A violação do validador de copy abre linha em `alerts` (D3b)
-- ---------------------------------------------------------------------------
-- O Judge 1 não olha disparo (D3). O validador determinístico de
-- `dispatch/variation.py` ocupa o lugar dele, e quando ele barra uma variação o
-- toque NÃO sai — mas um toque que não sai em silêncio é um funil que
-- emudeceu sem explicação.
--
-- `critical_violation` e não um tipo novo: o CHECK de `alerts.type` já tem o
-- valor certo, e ele descreve exatamente isto — o portão que substitui o juiz
-- pegou um texto que teria chegado a um contato com um número, um prazo, um
-- link ou uma promessa que a base aprovada não tinha.
--
-- Um alerta por toque, não por tentativa: o job sobe a escada de retentativas
-- até a DLQ, e três alertas idênticos para um mesmo toque transformariam o
-- sinal em ruído no exato lugar onde alguém precisa reagir.
create function internal.open_copy_violation_alert(
    p_tenant_id          uuid,
    p_scheduled_touch_id uuid,
    p_violations         text[]
)
    returns boolean
    language sql
    security definer
    set search_path = pg_catalog, public, internal
as $$
    insert into public.alerts (tenant_id, type, severity, title, payload)
    select p_tenant_id,
           'critical_violation',
           'critical',
           'Variação de copy barrada pelo validador determinístico',
           -- O TEXTO reprovado não entra: `alerts` é lido fora do Postgres e o
           -- conteúdo de uma mensagem é PII. O que entra é o toque (que leva a
           -- ele) e QUAIS regras quebraram, que é o diagnóstico.
           jsonb_build_object(
               'scheduled_touch_id', p_scheduled_touch_id,
               'violations', to_jsonb(p_violations))
     where not exists (
         select 1
           from public.alerts a
          where a.type = 'critical_violation'
            and a.status = 'open'
            and a.payload ->> 'scheduled_touch_id' = p_scheduled_touch_id::text
     )
    returning true;
$$;

comment on function internal.open_copy_violation_alert(uuid, uuid, text[]) is
    'D3b: variação barrada → o toque não sai e um humano fica sabendo. Um alerta por toque.';

revoke execute on function internal.open_copy_violation_alert(uuid, uuid, text[]) from public;
grant execute on function internal.open_copy_violation_alert(uuid, uuid, text[]) to worker_role;

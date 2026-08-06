-- E3 · S7 — o ritmo de entrega por número, e o tier da Cloud.
--
-- D10 divide "anti-ban" em duas metades pelo que elas SÃO: ritmo de entrega é do
-- sender (jitter, warm-up, teto diário) e conteúdo é do dispatch (variação de
-- copy). Esta migration é a metade do sender, e ela não contém regra nenhuma —
-- contém FATOS e a aritmética atômica de contá-los.
--
-- A regra mora em `queueing/antiban.py`, em Python, uma vez. Os números
-- canônicos (30–120s, 20→50→100, 300/dia, 80% do tier) chegam aqui como
-- PARÂMETRO ou não chegam: é a mesma disciplina que `internal.dispatch_touch`
-- adotou no S4, e ela existe porque uma segunda cópia de um número canônico é
-- uma divergência esperando o dia em que alguém mudar só uma das duas.
--
-- Duas coisas que o E1 anotou como dívida deste marco fecham aqui: as colunas
-- anti-ban do S2 ganham ESCRITOR (guarda sem alvo mente — coluna que nasce sem
-- quem a preencha é proteção decorativa, foi o achado do S3/S4), e
-- `claim_outbox_batch` passa a devolver os fatos do número junto com a linha,
-- porque o comentário do próprio tipo já dizia por quê: "sender que faz segunda
-- query abre porta para inconsistência — entre a query e o envio, o mundo muda".

-- ---------------------------------------------------------------------------
-- 1. Os contadores do número
-- ---------------------------------------------------------------------------
alter table public.channels_accounts
    -- Quantos PROATIVOS este número já fez no dia corrente, e qual é esse dia.
    -- Duas colunas em vez de uma tabela de log: o teto é diário e a pergunta é
    -- sempre "hoje", então guardar a série inteira seria guardar histórico que
    -- ninguém consulta para aplicar a regra. A métrica histórica é do S11, e
    -- sai da outbox, que é onde todo envio já passa.
    add column sends_today            integer not null default 0 check (sends_today >= 0),
    add column sends_day              date,
    -- Quando este número pode voltar a falar. O jitter de 30–120s, materializado
    -- — é do NÚMERO, não da mensagem.
    add column next_send_at           timestamptz,
    -- Quando a janela de 24h do tier começou a contar. Sem ela `tier_usage_24h`
    -- é um contador que só cresce, e um número saudável ficaria pausado para
    -- sempre depois do primeiro dia movimentado.
    add column tier_window_started_at timestamptz;

comment on column public.channels_accounts.sends_today is
    'Proativos enviados por este número no dia de sends_day. Escrito por internal.record_channel_send().';
comment on column public.channels_accounts.next_send_at is
    'Jitter anti-ban materializado (30–120s, CLAUDE.md). Reativa nunca espera por ele.';

-- ---------------------------------------------------------------------------
-- 2. O aceite do risco — a coluna do S2 ganha escritor
-- ---------------------------------------------------------------------------
-- A Evolution é canal NÃO oficial: usá-la é aceitar o risco de o número ser
-- banido, e quem aceita é o lojista. RNF-044 pede o registro; a D1 já
-- estabeleceu a forma de uma decisão que um humano responde por — função
-- SECURITY DEFINER, EXECUTE revogado de PUBLIC, `audit_log` na mesma
-- transação. O aceite e o seu registro não podem existir separados: um aceite
-- sem trilha é a nossa palavra contra a do lojista no dia do banimento.
create function internal.accept_channel_risk(
    p_channel_account_id uuid,
    p_actor_user_id      uuid default null
)
    returns boolean
    language plpgsql
    security definer
    set search_path = pg_catalog, public, internal
as $$
declare
    v_tenant_id uuid;
    v_type      text;
begin
    select tenant_id, type
      into v_tenant_id, v_type
      from public.channels_accounts
     where id = p_channel_account_id
       -- Idempotente: aceitar duas vezes não reescreve a data do primeiro
       -- aceite. A data É a prova, e movê-la apagaria desde quando ela vale.
       and risk_accepted_at is null
       for update;

    if not found then
        return false;
    end if;

    update public.channels_accounts
       set risk_accepted_at = now()
     where id = p_channel_account_id;

    insert into public.audit_log
        (tenant_id, actor_type, actor_user_id, action, target_type, target_id, payload)
    values
        (v_tenant_id,
         case when p_actor_user_id is null then 'system' else 'user' end,
         p_actor_user_id,
         'channel.risk_accepted',
         'channels_accounts',
         p_channel_account_id,
         jsonb_build_object('channel_type', v_type));

    return true;
end
$$;

comment on function internal.accept_channel_risk(uuid, uuid) is
    'RNF-044: o lojista aceita o risco de banimento de um canal não oficial. '
    'Aceite e trilha na mesma transação — um sem o outro não é aceite.';

-- ---------------------------------------------------------------------------
-- 3. O envio contado — e o tier que pausa a 80%
-- ---------------------------------------------------------------------------
-- Chamada pelo sender DEPOIS que o provedor aceitou. Antes seria reservar um
-- slot que talvez não se use; depois é contar o que de fato saiu, que é o que
-- tanto o teto quanto o tier do Meta medem.
--
-- Só PROATIVO conta, e isso não é economia de linha:
--
--   * o teto diário e o warm-up existem contra disparo em massa, que é o que
--     bane um número. Uma conversa que o contato começou não é disparo;
--   * o tier do Meta limita conversas INICIADAS PELO NEGÓCIO. Uma resposta
--     dentro da janela de atendimento não consome tier nenhum, então contá-la
--     pausaria proativos por causa de um movimento que o Meta nem cobra.
--
-- O `p_tier_pause_fraction` chega de `dispatch/ladder.TIER_PAUSE_FRACTION`, a
-- mesma constante que a escada usa para BLOQUEAR. Uma cópia do 0.8 aqui seria o
-- dia em que o alerta dispara num limiar e a pausa acontece noutro.
create function internal.record_channel_send(
    p_channel_account_id  uuid,
    p_proactive           boolean,
    p_next_send_at        timestamptz,
    p_tier_pause_fraction double precision
)
    returns void
    language plpgsql
    security definer
    set search_path = pg_catalog, public, internal
as $$
declare
    v_today     date := (now() at time zone 'utc')::date;
    v_account   public.channels_accounts%rowtype;
    v_was_below boolean;
begin
    select * into v_account
      from public.channels_accounts
     where id = p_channel_account_id
       for update;

    if not found then
        return;
    end if;

    if not p_proactive then
        return;
    end if;

    v_was_below := v_account.meta_tier is null
                or v_account.tier_usage_24h < p_tier_pause_fraction * v_account.meta_tier;

    update public.channels_accounts
       set sends_today = case when sends_day = v_today then sends_today + 1 else 1 end,
           sends_day   = v_today,
           next_send_at = coalesce(p_next_send_at, next_send_at),
           -- A janela de 24h do tier é rolante: vencida, o contador recomeça
           -- neste envio em vez de somar para sempre.
           tier_usage_24h = case
               when meta_tier is null then tier_usage_24h
               when tier_window_started_at is null
                 or tier_window_started_at <= now() - interval '24 hours' then 1
               else tier_usage_24h + 1
           end,
           tier_window_started_at = case
               when meta_tier is null then tier_window_started_at
               when tier_window_started_at is null
                 or tier_window_started_at <= now() - interval '24 hours' then now()
               else tier_window_started_at
           end
     where id = p_channel_account_id
    returning * into v_account;

    -- RF-035: a 80% do tier, pausa proativos + ALERTA. A pausa é da escada
    -- (rung `channel_paused_tier`, que já lê estas duas colunas); o que faltava
    -- era alguém alimentar o contador e avisar um humano. Só na TRAVESSIA: um
    -- alerta por envio, depois dos 80%, seria um alerta que ninguém lê.
    if v_account.meta_tier is not null
       and v_was_below
       and v_account.tier_usage_24h >= p_tier_pause_fraction * v_account.meta_tier
    then
        insert into public.alerts (tenant_id, type, severity, title, payload)
        values (v_account.tenant_id,
                'meta_tier',
                'warning',
                'Tier do WhatsApp em ' || round(100 * p_tier_pause_fraction) || '% — proativos pausados',
                -- Sem PII: nem telefone, nem conteúdo. Só o id da conta e os
                -- números que explicam a pausa.
                jsonb_build_object(
                    'channel_account_id', v_account.id,
                    'meta_tier', v_account.meta_tier,
                    'tier_usage_24h', v_account.tier_usage_24h));
    end if;
end
$$;

comment on function internal.record_channel_send(uuid, boolean, timestamptz, double precision) is
    'Conta o proativo que saiu: teto diário, jitter do próximo e tier do Meta. '
    'Reativa não conta — o tier do Meta mede conversa iniciada pelo negócio.';

-- ---------------------------------------------------------------------------
-- 4. Adiar não é falhar
-- ---------------------------------------------------------------------------
-- Um número esperando o jitter, ou que bateu o teto do dia, não teve um ERRO: a
-- linha não foi tentada. `mark_outbox_failed` seria a ferramenta errada em dois
-- sentidos — gravaria `last_error` num envio que ninguém tentou, e queimaria a
-- tentativa que o claim acabou de incrementar, de modo que um número em jitter
-- esgotaria as próprias retentativas esperando a vez.
--
-- Por isso a tentativa é DEVOLVIDA. `attempt_count` volta ao que era, e a linha
-- fica `pending` de novo com `next_attempt_at` no futuro exato que a regra pediu.
create function internal.defer_outbox_send(
    p_outbox_id   uuid,
    p_claim_token uuid,
    p_retry_in    interval
)
    returns boolean
    language plpgsql
    security definer
    set search_path = pg_catalog, internal
as $$
begin
    update internal.message_outbox
       set status = 'pending',
           next_attempt_at = now() + p_retry_in,
           locked_by = null,
           locked_until = null,
           request_started_at = null,
           attempt_count = greatest(attempt_count - 1, 0)
     where id = p_outbox_id
       and status = 'sending'
       and locked_by = p_claim_token::text;

    return found;
end
$$;

comment on function internal.defer_outbox_send(uuid, uuid, interval) is
    'A linha volta para pending sem gastar tentativa: adiar por ritmo não é falhar.';

-- ---------------------------------------------------------------------------
-- 5. O claim passa a carregar os fatos do número
-- ---------------------------------------------------------------------------
-- O tipo cresce em vez de o sender ganhar uma segunda consulta — o comentário
-- original do tipo já explicava por quê, e ele continua valendo palavra por
-- palavra. Os atributos novos entram DEPOIS dos oito existentes, então quem lê
-- por posição continua lendo o mesmo.
--
-- Recriar (em vez de `alter type add attribute`) é deliberado: a função depende
-- do tipo, e um `create or replace` da função com o tipo alterado por baixo é o
-- tipo de migration que funciona na máquina de quem escreveu. Drop e recreate,
-- na ordem, com os grants reafirmados — nada aqui é destrutivo para dado.
drop function internal.claim_outbox_batch(uuid, integer, interval);
drop type internal.claimed_send;

create type internal.claimed_send as (
    outbox_id           uuid,
    tenant_id           uuid,
    channel_type        text,
    channel_external_id text,
    to_phone_e164       text,
    payload             jsonb,
    idempotency_key     text,
    attempt_count       integer,
    -- --- o ritmo do número (S7) -------------------------------------------
    -- `kind` resolvido em booleano aqui, e não interpretado no Python: a
    -- pergunta "isto é proativo?" tem uma resposta só, e ela vem da coluna que
    -- a outbox já carrega. Deixar o runtime deduzir seria deixar dois lugares
    -- decidirem o que é um disparo.
    proactive           boolean,
    channel_account_id  uuid,
    risk_accepted       boolean,
    warmup_stage        integer,
    daily_cap           integer,
    sends_today         integer,
    next_send_at        timestamptz
);

comment on type internal.claimed_send is
    'Tudo que um envio precisa, numa linha só — inclusive o ritmo do número. '
    'Sender que faz segunda query é sender que abre porta para inconsistência: '
    'entre a query e o envio, o mundo muda.';

create function internal.claim_outbox_batch(
    p_claim_token uuid,
    p_limit       integer default 50,
    p_lease       interval default interval '60 seconds'
)
    returns setof internal.claimed_send
    language sql
    security definer
    set search_path = pg_catalog, public, internal
as $$
    with claimed as (
        select id
          from internal.message_outbox
         where status = 'pending'
           and next_attempt_at <= now()
         order by next_attempt_at
         for update skip locked
         limit p_limit
    ),
    marked as (
        update internal.message_outbox o
           set status = 'sending',
               locked_by = p_claim_token::text,
               locked_until = now() + p_lease,
               request_started_at = now(),
               attempt_count = o.attempt_count + 1
          from claimed
         where o.id = claimed.id
        returning o.*
    )
    select m.id,
           m.tenant_id,
           ch.type,
           ch.external_account_id,
           ct.phone_e164,
           m.payload,
           m.idempotency_key,
           m.attempt_count,
           m.kind in ('funnel_touch', 'followup'),
           ch.id,
           ch.risk_accepted_at is not null,
           ch.warmup_stage,
           ch.daily_cap,
           -- Zero quando o contador é de outro dia: o teto é diário, e um
           -- processo que atravessa a meia-noite não carrega a conta de ontem.
           case when ch.sends_day = (now() at time zone 'utc')::date
                then ch.sends_today else 0 end,
           ch.next_send_at
      from marked m
      join public.channels_accounts ch on ch.id = m.channel_account_id
      join public.contacts ct on ct.id = m.contact_id
$$;

-- ---------------------------------------------------------------------------
-- Quem executa
-- ---------------------------------------------------------------------------
revoke execute on function internal.claim_outbox_batch(uuid, integer, interval) from public;
grant execute on function internal.claim_outbox_batch(uuid, integer, interval) to sender_role;

revoke execute on function internal.defer_outbox_send(uuid, uuid, interval) from public;
grant execute on function internal.defer_outbox_send(uuid, uuid, interval) to sender_role;

revoke execute on function
    internal.record_channel_send(uuid, boolean, timestamptz, double precision) from public;
grant execute on function
    internal.record_channel_send(uuid, boolean, timestamptz, double precision) to sender_role;

-- O aceite é ato do lojista pelo hub (E5) e do admin pelo painel (E6); nenhum
-- papel do runtime o executa. Concedê-lo ao worker "por precaução" seria dar ao
-- processo automático a assinatura que existe justamente para ser humana.
revoke execute on function internal.accept_channel_risk(uuid, uuid) from public;

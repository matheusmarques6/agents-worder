-- E3 · S11 — os números do marco como FATO consultável.
--
-- O E5 e o E6 vão perguntar as mesmas quatro coisas: quantos toques saíram,
-- quantos morreram e POR QUÊ, quanto voltou em dinheiro, e como está o número.
-- Se cada tela inventar a sua consulta, o mesmo número aparece diferente em dois
-- lugares e ninguém sabe qual está certo — e o motivo do cancelamento, que é o
-- diagnóstico inteiro deste marco, vira nove `count(*)` que cada autor agrupa
-- como bem entende. Foi exatamente para esta view existir que o vocabulário da
-- escada entrou no `cancel_reason` no S2, em vez dos quatro valores do
-- dicionário: achatar nove motivos em quatro apagaria a única métrica que
-- distingue "o funil funcionou" de "o produto está quebrado".
--
-- **`security_invoker = true` em todas.** É a linha que faz a RLS da tabela por
-- baixo valer sobre a view, e sem ela a view rodaria com os privilégios de quem a
-- criou — o mesmo defeito de escrever `WHERE tenant_id = ...` à mão, só que
-- invisível no call site. O `CLAUDE.md` proíbe a versão visível; a invisível é
-- pior.
--
-- **Sem PII.** Estas views são lidas por tela e, quando o B-2/B-3 chegar,
-- raspadas para métrica. Nenhuma expõe telefone, nome ou conteúdo. A view do
-- canal é a mais delicada e por isso é a única que ganha GRANT por COLUNA:
-- `channels_accounts` carrega o telefone e a referência do Vault na mesma linha
-- dos contadores, e um `grant select on channels_accounts` levaria os dois para a
-- Data API de brinde.
--
-- O que NÃO está aqui: profundidade da `q_scheduled` e da sua DLQ. Isso é estado
-- do pgmq, mora em `internal` (ADR-11 mantém fila fora da Data API) e o consumidor
-- natural é o exportador OTLP — que depende do Logfire e do Grafana Cloud
-- (pendências B-2/B-3) e não existe neste passo.

-- ---------------------------------------------------------------------------
-- 1. Os toques, por desfecho e por motivo
-- ---------------------------------------------------------------------------
-- O dia é bucketizado em UTC e não no fuso da sessão: `date_trunc` sem `at time
-- zone` lê o `TimeZone` de quem conecta, e o mesmo número mudaria de dia entre
-- duas telas. Localizar é trabalho do hub, sobre um bucket que não se move.
create view public.metrics_touches
    with (security_invoker = true)
as
select t.tenant_id,
       t.funnel_id,
       f.occasion,
       date_trunc('day', t.created_at at time zone 'UTC') as day,
       t.status,
       -- NULL para tudo que não é cancelamento. É o eixo do diagnóstico: um
       -- `cancelled` sem motivo é impossível por CHECK desde o S2
       -- (`scheduled_touches_cancel_is_whole`), então esta coluna nunca mente.
       t.cancel_reason,
       count(*) as touches
  from public.scheduled_touches t
  join public.funnels f on f.id = t.funnel_id
 group by t.tenant_id, t.funnel_id, f.occasion,
          date_trunc('day', t.created_at at time zone 'UTC'), t.status, t.cancel_reason;

comment on view public.metrics_touches is
    'Toques agendados/enviados/cancelados POR MOTIVO, por funil e por dia UTC (E3 S11). '
    'security_invoker: a RLS de scheduled_touches é a mesma aqui. Sem contato e sem telefone.';

-- ---------------------------------------------------------------------------
-- 2. O toque preso — onde a pessoa avisada vai achar QUAL
-- ---------------------------------------------------------------------------
-- O alerta `touch_stuck` do S11 diz "há N presos e o pior desde X". Sem esta
-- view ele mandaria alguém escrever SQL às três da manhã. Grão de linha, e não
-- de contagem, porque a ação (achar o job na DLQ) é por toque.
--
-- Isto é uma LEITURA. Nada aqui devolve o toque para `pending`: um segundo
-- relógio sobre as linhas que o dispatcher possui pode reenviar o que já saiu, e
-- essa é a duplicidade que o compare-and-set do S4 existe para impedir.
create view public.metrics_stuck_touches
    with (security_invoker = true)
as
select t.tenant_id,
       t.id as scheduled_touch_id,
       t.funnel_id,
       t.touch_number,
       t.claimed_by,
       t.claimed_at,
       floor(extract(epoch from now() - coalesce(t.claimed_at, t.created_at)))::bigint
           as age_seconds
  from public.scheduled_touches t
 where t.status = 'enqueued';

comment on view public.metrics_stuck_touches is
    'Toques reivindicados que nunca viraram nada (achado do S4), com a idade. É o detalhe do '
    'alerta touch_stuck. Só leitura: devolver um toque para pending seria um segundo relógio.';

-- ---------------------------------------------------------------------------
-- 3. As conversões atribuídas
-- ---------------------------------------------------------------------------
-- A moeda é chave de agrupamento e não coluna decorativa: somar BRL com USD
-- produz um número que não é dinheiro nenhum.
create view public.metrics_conversions
    with (security_invoker = true)
as
select c.tenant_id,
       c.funnel_id,
       date_trunc('day', c.attributed_at at time zone 'UTC') as day,
       c.currency,
       count(*)     as conversions,
       sum(c.amount) as amount
  from public.funnel_conversions c
 group by c.tenant_id, c.funnel_id,
          date_trunc('day', c.attributed_at at time zone 'UTC'), c.currency;

comment on view public.metrics_conversions is
    'Receita recuperada (D8) por funil, dia UTC e moeda. A moeda agrupa: somar duas produz '
    'um número que não é dinheiro nenhum.';

-- ---------------------------------------------------------------------------
-- 4. A saúde do número — tier e warm-up
-- ---------------------------------------------------------------------------
-- `tier_usage_fraction` vem pronta porque a escada pausa numa fração (0,8,
-- RF-035) e uma tela que dividisse sozinha seria a terceira cópia da mesma
-- conta. NULL quando não há tier: a Evolution não tem um (D10), e zero diria
-- "está folgado", que é uma afirmação sobre um limite que não existe.
create view public.metrics_channel_health
    with (security_invoker = true)
as
select ca.tenant_id,
       ca.id   as channel_account_id,
       ca.type as channel_type,
       ca.status,
       ca.meta_tier,
       ca.tier_usage_24h,
       case when ca.meta_tier is null then null
            else round(ca.tier_usage_24h::numeric / ca.meta_tier, 4)
       end as tier_usage_fraction,
       ca.tier_window_started_at,
       ca.warmup_stage,
       ca.daily_cap,
       -- Zero quando o contador é de outro dia: o teto é diário, e um processo
       -- que atravessa a meia-noite não carrega a conta de ontem. A mesma regra
       -- que `claim_outbox_batch` aplica desde o S7 — e ela está escrita duas
       -- vezes porque são duas perguntas (o que o sender pode fazer agora, o que
       -- a tela mostra), não porque alguém copiou um número canônico.
       case when ca.sends_day = (now() at time zone 'utc')::date
            then ca.sends_today else 0 end as sends_today,
       ca.next_send_at,
       ca.risk_accepted_at is not null as risk_accepted
  from public.channels_accounts ca;

comment on view public.metrics_channel_health is
    'Uso do tier do Meta e estágio de warm-up por número (E3 S11). Sem telefone, sem '
    'external_account_id e sem vault_secret_id: a view é lida por tela e raspada por métrica.';

-- ---------------------------------------------------------------------------
-- Quem lê — e o que `authenticated` passa a poder ver em `channels_accounts`
-- ---------------------------------------------------------------------------
-- As três primeiras views leem tabelas que o hub já lia (`scheduled_touches`,
-- `funnels`, `funnel_conversions`, todas com GRANT e política de membro desde o
-- S2). A quarta lê `channels_accounts`, que nunca teve nem uma nem outra — a
-- configuração do canal era só do runtime.
--
-- O GRANT é por COLUNA, e essa é a decisão do arquivo. Um `grant select on
-- public.channels_accounts to authenticated` seria uma linha mais curta e
-- entregaria `phone_e164`, `external_account_id` e `vault_secret_id` à Data API
-- junto com os contadores. A referência do Vault não é o segredo, mas expô-la à
-- borda pública é gratuito, e o `CLAUDE.md` é explícito sobre segredo só por
-- função com escopo. Colunas de PII e de credencial ficam de fora por nome, e o
-- dia em que alguém acrescentar uma tem de acrescentá-la aqui de propósito.
grant select (id, tenant_id, type, status, meta_tier, tier_usage_24h,
              tier_window_started_at, warmup_stage, daily_cap,
              sends_today, sends_day, next_send_at, risk_accepted_at, created_at)
    on public.channels_accounts to authenticated;

-- A política que faltava. Sem ela o GRANT acima devolveria o número de todo
-- mundo — e a ordem importa: política e grant no mesmo arquivo, pela regra que
-- as migrations 0001 e 0002 escreveram (uma tabela que existe por uma migration
-- que seja com GRANT e sem política esteve legível cross-tenant na história do
-- schema).
create policy channels_accounts_member_read on public.channels_accounts
    for select to authenticated
    using (tenant_id in (select public.user_tenant_ids()));

-- As views em si. `authenticated` para o hub e o admin; `worker_role` porque o
-- runtime é o outro leitor legítimo destes mesmos fatos e a view é o lugar onde
-- a conta está escrita uma vez.
grant select on public.metrics_touches, public.metrics_stuck_touches,
                public.metrics_conversions, public.metrics_channel_health
    to authenticated, worker_role;

-- `anon` não recebe nada, e isto é dito em vez de assumido: a Data API expõe o
-- schema `public` inteiro, e uma view nova sem GRANT explícito é uma view que
-- depende do default do banco para não vazar.
revoke all on public.metrics_touches, public.metrics_stuck_touches,
               public.metrics_conversions, public.metrics_channel_health
    from anon;

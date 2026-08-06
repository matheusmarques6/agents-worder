-- E3 · S6 — "não me mande mais mensagem", pelas três vias, com um dono só.
--
-- O RF-033 tem três portas: o botão Bloquear de um disparo a contato novo, o
-- silêncio depois de três funis distintos, e o opt-out por intenção que o
-- agente detecta. As três escrevem na MESMA tabela, e é isso que este arquivo
-- garante — não porque três funções fossem caras, mas porque três escritores
-- com três regras produzem, no pior dia, três respostas para "este contato está
-- bloqueado?".
--
-- **A decisão de arquitetura que este passo fecha (achado do S2).**
-- `public.suppression_list` é a AUTORIDADE; `public.contacts.opt_status` passa a
-- ser PROJEÇÃO dela. Hoje as duas colunas respondem a mesma pergunta e nada
-- impede que divirjam: a escada lê a lista, o hub e o prompt leem o
-- `opt_status`, e um bloqueio gravado sem atualizar a outra coluna apareceria
-- como "pendente" na tela do lojista enquanto a plataforma o tratava como
-- bloqueado. A projeção é feita por TRIGGER e não pelo código que escreve,
-- porque um dever que cada escritor precisa lembrar é um dever que o quarto
-- escritor esquece — e o quarto escritor já tem data: o hub do E5.
--
-- `core/dicionario-de-dados.md` §4.1/§4.4 é atualizado no mesmo PR.

-- ---------------------------------------------------------------------------
-- A projeção: opt_status segue suppression_list, sempre
-- ---------------------------------------------------------------------------
-- Só o valor `blocked` é derivado. `pending` e `authorized` são consentimento
-- expresso pelo contato e não existem em lugar nenhum além desta coluna — a
-- lista de supressão sabe quem disse "não", não sabe quem disse "sim".
--
-- No DELETE o contato volta a `pending`, nunca a `authorized`: desfazer uma
-- supressão devolve a pergunta, não uma autorização que ninguém deu. Fechar na
-- direção estrita é a única direção em que uma proteção pode se mover sozinha.
create function internal.project_opt_status()
    returns trigger
    language plpgsql
    set search_path = pg_catalog, public
as $$
begin
    if tg_op = 'INSERT' then
        update public.contacts
           set opt_status = 'blocked'
         where id = new.contact_id
           and opt_status <> 'blocked';
        return new;
    end if;

    update public.contacts
       set opt_status = 'pending'
     where id = old.contact_id
       and opt_status = 'blocked';
    return old;
end
$$;

comment on function internal.project_opt_status() is
    'contacts.opt_status = projeção de suppression_list (S6). A lista manda; a coluna reflete.';

create trigger suppression_list_projects_opt_status
    after insert or delete on public.suppression_list
    for each row execute function internal.project_opt_status();

comment on column public.contacts.opt_status is
    'PROJEÇÃO de public.suppression_list (autoridade). `blocked` é derivado por trigger; '
    '`pending`/`authorized` são o consentimento expresso, que só esta coluna guarda.';

-- ---------------------------------------------------------------------------
-- internal.suppress_contact — vias (a) bloquear e (c) opt-out por intenção
-- ---------------------------------------------------------------------------
-- SECURITY INVOKER, e é a metade que importa: o tenant sai da LINHA DO CONTATO
-- lida sob a RLS de quem chama, nunca de um argumento. Quem chama pela via (c)
-- é uma tool, e uma tool recebe argumentos escolhidos por um modelo que acabou
-- de ler a mensagem de um estranho — contato que a RLS não mostra simplesmente
-- não existe, e a função devolve NULL em vez de escrever em nome de outro.
--
-- Três desfechos, como dado: NULL = não é deste tenant · false = já estava
-- suprimido · true = a linha nasceu agora. O `false` não gera linha de
-- auditoria: o contato pediu duas vezes e o fato é um só.
create function internal.suppress_contact(
    p_contact_id uuid,
    p_reason     text,
    p_created_by text
)
    returns boolean
    language plpgsql
    set search_path = pg_catalog, public, internal
as $$
declare
    v_tenant_id uuid;
    v_rows      integer;
begin
    select tenant_id into v_tenant_id from public.contacts where id = p_contact_id;

    if v_tenant_id is null then
        return null;
    end if;

    insert into public.suppression_list (tenant_id, contact_id, reason, created_by)
    values (v_tenant_id, p_contact_id, p_reason, p_created_by)
    on conflict (tenant_id, contact_id) do nothing;

    get diagnostics v_rows = row_count;
    if v_rows = 0 then
        return false;
    end if;

    -- RNF-044: consentimento e oposição REGISTRADOS. A linha em
    -- `suppression_list` é o estado; esta é a trilha de quem o causou e quando,
    -- e é append-only por privilégio (ninguém tem UPDATE nem DELETE nela).
    insert into public.audit_log
        (tenant_id, actor_type, action, target_type, target_id, payload)
    values
        (v_tenant_id, 'system', 'suppression.' || p_reason, 'contact', p_contact_id,
         jsonb_build_object('reason', p_reason, 'created_by', p_created_by));

    return true;
end
$$;

comment on function internal.suppress_contact(uuid, text, text) is
    'RF-033 (a) e (c). Tenant resolvido da linha do contato sob RLS, nunca de argumento. '
    'NULL = não é deste tenant · false = já suprimido · true = suprimido agora.';

-- ---------------------------------------------------------------------------
-- internal.authorize_contact — a outra metade do botão
-- ---------------------------------------------------------------------------
-- Nunca remove uma supressão, e isso é regra e não omissão: um toque de botão
-- não desfaz o registro de alguém tendo pedido para não ser incomodado.
-- Autorizar é o caminho de quem ainda não disse nada; desbloquear é ação de
-- operador, com tela e trilha (E5).
create function internal.authorize_contact(p_contact_id uuid)
    returns boolean
    language plpgsql
    set search_path = pg_catalog, public, internal
as $$
declare
    v_tenant_id uuid;
    v_rows      integer;
begin
    select tenant_id into v_tenant_id from public.contacts where id = p_contact_id;

    if v_tenant_id is null then
        return null;
    end if;

    if exists (
        select 1 from public.suppression_list s
         where s.tenant_id = v_tenant_id and s.contact_id = p_contact_id
    ) then
        return false;
    end if;

    update public.contacts
       set opt_status = 'authorized'
     where id = p_contact_id
       and opt_status <> 'authorized';

    get diagnostics v_rows = row_count;
    if v_rows = 0 then
        -- Já autorizado. O consentimento é o mesmo; a trilha não ganha uma
        -- linha por cada vez que alguém toca de novo no mesmo botão.
        return true;
    end if;

    insert into public.audit_log
        (tenant_id, actor_type, action, target_type, target_id, payload)
    values
        (v_tenant_id, 'system', 'consent.authorized', 'contact', p_contact_id,
         jsonb_build_object('source', 'button'));

    return true;
end
$$;

comment on function internal.authorize_contact(uuid) is
    'RF-033 (a), botão Autorizar. NULL = não é deste tenant · false = existe supressão e ela manda · '
    'true = o contato está autorizado.';

-- ---------------------------------------------------------------------------
-- internal.suppress_silent_contacts — via (b), o silêncio virando fato
-- ---------------------------------------------------------------------------
-- SECURITY DEFINER e SEM parâmetro de filtro, no molde de
-- `internal.claim_due_touches` e da leitura do ADR-11 que este marco já fixou:
-- a varredura é cross-tenant por natureza, e um chamador que pudesse pedir "só
-- o tenant X" seria consulta cross-tenant arbitrária com outro nome.
--
-- "Sem resposta" é medido CONTRA A ÚLTIMA MENSAGEM DE ENTRADA do contato, não
-- toque a toque: contam os toques enviados DEPOIS da última vez que ele falou.
-- É a leitura literal de "silêncio após 3 disparos em funis distintos" e é
-- também o que dispensa um período de carência — que seria um número inventado
-- sem fonte em nenhum canônico. Quem responde qualquer coisa zera a conta.
--
-- O limiar chega como PARÂMETRO porque o 3 do RF-033 mora em
-- `agents_runtime.dispatch.consent.SILENCE_FUNNEL_THRESHOLD`: um literal aqui
-- seria a segunda cópia de um número canônico, livre para divergir em silêncio
-- — a mesma disciplina das janelas da escada no S4.
create function internal.suppress_silent_contacts(p_distinct_funnels integer)
    returns integer
    language sql
    security definer
    set search_path = pg_catalog, public, internal
as $$
    with silent as (
        select t.tenant_id, t.contact_id
          from public.scheduled_touches t
         where t.status = 'sent'
           and t.sent_at > coalesce(
                   (select max(m.created_at)
                      from public.messages m
                      join public.conversations c on c.id = m.conversation_id
                     where c.contact_id = t.contact_id
                       and m.direction = 'inbound'),
                   '-infinity'::timestamptz)
         group by t.tenant_id, t.contact_id
        having count(distinct t.funnel_id) >= p_distinct_funnels
    ),
    removed as (
        insert into public.suppression_list (tenant_id, contact_id, reason, created_by)
        select tenant_id, contact_id, 'no_response_after_3', 'system' from silent
        -- Já suprimido por outra via: a lista é estado, não log, e a primeira
        -- razão continua sendo a verdadeira.
        on conflict (tenant_id, contact_id) do nothing
        returning tenant_id, contact_id
    ),
    audited as (
        insert into public.audit_log
            (tenant_id, actor_type, action, target_type, target_id, payload)
        select tenant_id, 'system', 'suppression.no_response_after_3', 'contact', contact_id,
               jsonb_build_object(
                   'reason', 'no_response_after_3',
                   'created_by', 'system',
                   'distinct_funnels', p_distinct_funnels)
          from removed
        returning 1
    )
    select coalesce(count(*), 0)::integer from audited
$$;

comment on function internal.suppress_silent_contacts(integer) is
    'RF-033 (b): três funis distintos tocados desde a última mensagem do contato → supressão gravada. '
    'Cross-tenant e sem filtro, no molde de claim_due_touches (ADR-11).';

-- ---------------------------------------------------------------------------
-- Quem executa
-- ---------------------------------------------------------------------------
revoke execute on function internal.suppress_contact(uuid, text, text) from public;
revoke execute on function internal.authorize_contact(uuid) from public;
revoke execute on function internal.suppress_silent_contacts(integer) from public;

-- O worker, e ninguém mais. `sender_role` drena a outbox; a lista de supressão
-- é lida pela escada antes de existir um item para ele drenar.
grant execute on function internal.suppress_contact(uuid, text, text) to worker_role;
grant execute on function internal.authorize_contact(uuid) to worker_role;
grant execute on function internal.suppress_silent_contacts(integer) to worker_role;

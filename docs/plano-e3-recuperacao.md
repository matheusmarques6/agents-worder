# Plano E3 — Recuperação completa, test-first

**Marco:** E3 · **Duração estimada:** 12–17 dias úteis (o `ordem-de-execucao` diz 7–10 — ver §9) · **Pré-requisito:** E2 fechado em código

Fontes canônicas: `core/arquitetura-plataforma-agentes-whatsapp.md` (ADR-3, ADR-4, ADR-5, ADR-8, fluxos 5.2–5.5) · `core/requisitos-e-entidades.md` (RF-030 a RF-036, RF-070, RF-073) · `core/dicionario-de-dados.md` §3.3–3.5, §4.4, §5.4–5.5 · `core/plano-de-testes.md` (O5, A5, A6) · `core/testes-e-cicd.md` §3.3 cenário 11 · `core/ordem-de-execucao.md` §E3. Este documento é o S0 do marco; o que aqui diverge dos canônicos está marcado como decisão e **exige atualizar o doc no mesmo PR**.

---

## 1. O que é o marco

O E1 provou o motor com texto fixo; o E2 trocou o texto fixo pelo agente real. O E3 é o **produto que o cliente compra**: o evento de abandono vira um funil com cadência, cada toque atravessa uma escada de proteção antes de existir, o pagamento mata o funil, a resposta do contato converte em conversa normal, e o segundo canal (Evolution) entra com anti-ban.

A frase que resume o marco: **no E1 e no E2 o sistema respondia; no E3 ele fala primeiro** — e falar primeiro é onde se banem números, se irritam contatos e se cobra quem já pagou. Por isso o marco é feito de portões, não de features: cada regra de proteção nasce como unitário vermelho antes de existir um toque que ela possa bloquear.

## 2. O que o E2 ainda deve (pré-requisito real)

O E3 não abre com o E2 pela metade. Ordem de fechamento:

| Item | Estado | Bloqueia o E3? |
|---|---|---|
| S9b — pós-envio, `q_evals`, auto-correção | **meio feito** (branch `e2/s9b-post-hoc`): o medidor de perigo, a migration e as suítes `unit`/`db` existem; **o consumidor de `q_evals`, o `post_hoc` e a auto-correção no runtime não** | **sim** — o E3 escreve na outbox por caminhos novos e precisa da avaliação já costurada. Vira o passo **P1** da execução |
| S10 — observabilidade / T4 | pendente (B-2/B-3 do Bruno) | não — mesmo tratamento do E1: prova pendente e explícita |
| PR-EF — dívida de teste da Edge Function | pendente | **sim, e antecipada para logo depois do S6** (era "antes do S8"): a partir do S6 aquela porta carrega **consentimento**, não só conteúdo — se o mapeamento do `button_reply` quebrar, o toque em Bloquear vira uma mensagem sem texto e a recusa de uma pessoa some em silêncio no transporte. Isso é pior que qualquer coisa que o S8 possa quebrar (achado do S6) |
| S11/S12 — pack real e provas | pendentes de credenciais | não — correm em paralelo ao E3 quando as chaves chegarem |

## 2.1 Duas regras que a execução do marco produziu (valem daqui em diante)

1. **Guarda sem alvo mente.** Coluna ou parâmetro que nasce num passo sem escritor no mesmo passo produz proteção decorativa: o S3 criou `start_funnel_run(p_order_id)` sem ninguém preenchê-lo, e o S4 construiu contra ele uma guarda inteira — docstring, conjunto no CAS, corrida encenada — que em produção sempre revalidava contra NULL, com o teste se auto-alimentando por um `update` à mão. Dois passos verdes sobre um caminho que produção nunca exercitava. É o espelho da doutrina que o repositório já tem ("config sem consumidor mente"), e o S5 fechou o buraco espelhando o pedido também no ramo do abandono.
2. **Exemplo de caso negativo nunca é o caso positivo do passo seguinte.** O E1 escreveu "tipo não suportado é descartado" ilustrando com `order_paid` — o tipo que o S5 passa a suportar. O invariante sobreviveu, o exemplo virou bomba-relógio, e o passo teve que parar para trocar uma linha.

## 3. As duas leis (vigentes, com uma emenda)

1. **Proibido mudar testes existentes.** O E3 é construção nova sobre costuras prontas: o handler de `q_domain_events` (E1), a outbox e o sender (E1), o responder (E2). Todo teste novo é arquivo novo.
   **Emenda D6 (única exceção, declarada):** o toque de texto fixo do E1 é **andaime rotulado como tal na própria migration** (`20260803000001`, linhas 3–8: "This is deliberately NOT the E3 funnel"). Ele é substituído no S3 e os testes que afirmavam o texto fixo morrem junto com ele, num commit único e com decisão registrada. A Lei 1 protege invariantes; ela não obriga a manter andaime que o próprio autor declarou provisório. **Os invariantes do handler sobrevivem intactos** (`already_applied`, `invalid_payload`, `no_channel`, evento inexistente levanta) — se algum deles cair no S3, o passo para.
2. **Rodar testes é obrigatório em cada passo.** Comandos em cada S; verdes antes do PR; `-m pipeline` local sempre que o runtime for tocado (com `docker compose stop runtime` antes — decisão 90).

## 4. Decisões fixadas

| # | Decisão | Estado |
|---|---|---|
| **D1** | **O teto da plataforma vive no banco.** `tenants.proactive_max_per_contact_24h smallint not null default 1 check (value between 1 and 4)` — o teto do RF-034 vira CHECK, não regra de aplicação. Afrouxar é **exclusivo do admin**: a coluna é escrita só por `internal.set_proactive_cap(tenant, value, actor)` (SECURITY DEFINER, EXECUTE revogado de PUBLIC, grava `audit_log`), e o `UPDATE` direto da coluna é revogado do caminho do lojista. O hub do lojista só aperta. Mesmo princípio do Judge 1 fixo: **trava de segurança não enfraquece por config de cliente** | **fixada (Bruno, 2026-08-06)** |
| **D2** | **A escada decide em Python puro; a gravação revalida.** As regras (supressão, quota, staleness, limites) são função pura, determinística, com **motivo como dado** — irmã do think-gate (S4 do E2) e do medidor de perigo (S9b). A decisão devolve também os **fatos que a sustentaram** (guards), e a inserção na outbox é um **compare-and-set** cujo `WHERE` revalida cada guard na mesma transação curta. Fato mudou entre decidir e gravar (mensagem nova, pagamento, bloqueio) → nada sai, o toque é cancelado com motivo. É a doutrina do ADR-6 aplicada ao proativo: regra pura fora da transação, atomicidade dentro dela, **sem duplicar a regra em SQL** | fixada |
| **D3** | **O Judge 1 é do tempo real. Disparo e campanha não passam por ele** — decisão do Bruno em 2026-08-06, contra a recomendação original deste plano. Consequências, todas obrigatórias: (a) a lei do `CLAUDE.md` e a arquitetura passam a dizer **"toda resposta reativa passa pelo Judge 1"**, no mesmo PR do S7 — lei que o código contradiz é pior que lei nenhuma; (b) o lugar do juiz é ocupado por um **validador determinístico de copy**: a variação anti-ban só pode variar o `copy_base` e **não pode introduzir número, prazo, link ou promessa que a base não tenha**; violação → o toque não sai e abre linha em `alerts`; (c) o item da outbox grava `payload.generated: true\|false`, para a auditoria separar copy gerada de template aprovado. **Risco residual registrado e aceito:** é o único ponto do produto onde texto de LLM alcança um contato sem portão de LLM — o validador é determinístico e a base é aprovada por humano, mas ele confere forma, não intenção | **fixada (Bruno, 2026-08-06)** |
| **D4** | **`orders` e `customers` nascem no E3; `products` não.** O cancelamento por pagamento, a atribuição de receita, a camada `customer_context` do RF-010 (que a decisão 88b deixou construída e sem consumidor) e as tools de pedido dependem do espelho de pedidos. Catálogo é RF-042 — nasce com as telas de upload/Sheets no E5 | fixada |
| **D5** | **A reconciliação chama a MESMA `ingest_webhook`.** Um segundo caminho de escrita seria uma segunda idempotência para manter em dia. O poll traduz a resposta da plataforma no formato do webhook e entrega na mesma porta — e a asserção do marco é que replay 3x pelos **dois** caminhos produz exatamente 1 efeito | fixada |
| **D6** | **O toque fixo do E1 é aposentado no S3** (emenda da Lei 1, §3) | fixada |
| **D7** | **Cancelamento tem dois relógios diferentes.** `order_paid` cancela **na hora**, pelo handler de domínio — é dinheiro do contato e a promoção por idade do ADR-4 existe exatamente para ele. Resposta do contato **não ganha gatilho no caminho quente**: nada na ingestão precisa conhecer funil. Quem decide é a escada no momento do disparo, que já lê a mensagem mais recente (staleness) e grava `cancel_reason='stale_newer_message'` — **o vocabulário é o da escada, não um sinônimo** (achado do S2: dois valores para o mesmo fato partem a métrica do S11 tão bem quanto achatar nove em quatro). Uma regra a menos no caminho de milissegundos, e a autoridade num lugar só | fixada |
| **D8** | **Atribuição de receita é fato gravado, não consulta.** Pedido pago dentro da janela (`tenants.attribution_window_hours`, default 24) depois de um toque enviado → linha em **`funnel_conversions`** (tenant, contact, funnel, touch, order, valor, atribuído em). Recalcular depois é impossível: `messages` tem TTL rolante de 12 meses e a métrica de receita recuperada precisa sobreviver à purga. **É tabela nova, fora do dicionário — entra em `core/dicionario-de-dados.md` no mesmo PR do S2** | **fixada (Bruno, 2026-08-06)** |
| **D9** | **`quota` nasce como ponto de aplicação com default ilimitado** (RF-073). A tabela `quota_rules` do dicionário §7.2 **não** nasce — placeholder sem regra é tabela que ninguém testa. O que nasce é a chamada dentro da escada e um teste de que ela é feita: ponto de aplicação que ninguém exercita apodrece em silêncio até o dia em que planos existirem | fixada |
| **D2-bis** | **Emenda da D2, escrita depois de construí-la (achado do S4).** A D2 descreve "a escada decide, a gravação revalida" como duas travas independentes. Na prática o CAS revalida **todas** as rungs e o caminho de conflito re-roda a escada só para nomear o motivo — então **o CAS é a aplicação e a escada é otimização mais vocabulário**, com uma exceção: **quota**, a única rung que o CAS não consegue reafirmar porque a D9 não cria tabela. Isso ficou visível porque a sabotagem-coroa do S4 (remover a escada do caminho) mediu **zero testes caindo** na primeira medição; o teste que faltava foi escrito e a segunda medição deu 2. Registrado porque é o tipo de verdade que alguém descobre no E5 mexendo no handler — e porque o valor real da separação é outro: os limiares (24h, 72h, 80%) viajam como **parâmetro** para o SQL, então não existe número mágico duplicado, só fato reafirmado | **fixada na execução (achado do S4)** |
| **D11** | **Todo proativo sai por um caminho só: a escada.** Achado do S1 durante a execução — a versão original do S3 mandava o primeiro toque direto para a outbox e só os seguintes para `scheduled_touches`, o que fazia o **primeiro** toque de todo funil pular a escada. O RF-033 diz que a lista de supressão é checada antes de **TODO** envio proativo e o RF-034 soma **todas as origens** na janela de 24h; um caminho rápido que escapa das duas é o bug que o cenário 11 acharia tarde. Então `start_funnel_run` cria **apenas** `scheduled_touches`, com o toque nº 1 vencido (`due_at = now()`), e o dispatcher do S4 é a única porta de saída. Custo: até um tique de latência no primeiro toque — irrelevante, porque a cadência do funil já decide em minutos. Ganho: um ponto de aplicação, não dois | **fixada na execução (achado do S1)** |
| **D12** | **A escada é mais estrita que a letra do RF-032.** O requisito manda checar staleness "antes de processar evento atrasado (> 5 min)". A escada checa **sempre**: o "> 5 min" descreve quando a checagem é necessária (drenagem pós-queda), não licença para pular proteção em evento fresco — e com D11 o caminho fresco virou o caminho normal. Ser mais estrito só suprime envio, nunca cria um. **O RF-032 ganha a frase de esclarecimento no PR do S2** | **fixada na execução (achado do S1)** |
| **D10** | **Anti-ban é do sender; variação de copy é do dispatch.** O jitter, o warm-up e o teto diário são ritmo de entrega por número — vivem no sender, que é o único que fala com a API (ADR-8). A variação de copy é conteúdo e precisa estar **decidida antes** da outbox, porque a coluna `payload` é "o conteúdo final a enviar, com variação anti-ban já aplicada" (dicionário §5.3). Misturar os dois faria o sender gerar conteúdo — e aí ele passaria a precisar de Judge, de LLM e de tenant | fixada |

## 5. Toolset que entra no E3

O E2 fechou com duas tools **incondicionais** (`search_knowledge`, `get_customer_context`) porque não havia escolha a fazer (decisão 88a). O E3 traz a primeira escolha real do modelo, agora que existem pedidos:

`get_order` (por id ou o mais recente do contato) · `get_tracking` (lê `orders.tracking_code`/`tracking_status` do espelho) · `escalate_to_human` · `record_optout` (o modelo detecta a intenção, a tool executa o efeito). Toda tool mantém a disciplina do S7 do E2: valida tenant e autorização ela mesma, transação curta própria com `SET LOCAL`, execução gravada em `tool_calls`.

**Rastreio por API externa não entra** — é a pendência nº 3 da arquitetura (17TRACK/Correios, custo não validado). O E3 responde com o que o espelho sabe; quando a decisão existir, o adaptador entra atrás da mesma tool.

## 6. Escopo negativo do E3

Onboarding, OAuth e Embedded Signup = E4 · gerador de prompt = E4 · telas (funis, dashboard de recuperação, catálogo) = E5 · takeover/inbox e cenário 12 = E5 · flywheel = E6 sob ADR-12 · purgas e cenário 13 = E6 · Nuvemshop e Yampi = E8 · `products` e RF-042 = E5 · API de rastreio = pendência nº 3 · **follow-up proativo dentro da conversa (RF-018) = E5**, junto da tela que o liga: é opt-in, nasce desligado, e config sem tela é feature que ninguém ativa — a escada já o cobre no dia em que nascer. Um PR que traga qualquer um desses volta.

---

## 7. Os passos

Cada S fecha com os comandos listados verdes e vira PR pelo gate (branch → PR → 4 checks → merge). Vermelho primeiro em tudo que especifica comportamento.

### S0 · Plano fixado (este documento) — 0,5d
Decisões D1/D3/D8 sobem para o Bruno; as demais ficam. Sem código.

### S1 · A escada de proteção, pura — antes de existir toque (1,5d)
`dispatch/ladder.py`: `decide(snapshot) → Decision(allow|deny, reason, guards)`. Ordem canônica do `CLAUDE.md`, sem invenção: **supressão → quota → staleness → limites**. Motivos como enum de dado — `suppressed_block | suppressed_silence | suppressed_optout | quota_exceeded | stale_newer_message | stale_order_paid | rate_limit_24h | funnel_cooldown_72h | channel_paused_tier | allowed`. Relógio injetável: 24h, 72h e a idade de 5 min do RF-032 testados sem espera real. Nada de I/O — a escada não sabe o que é um banco.
→ Testes (`unit`): um por regra, vermelho primeiro · **precedência afirmada** (contato suprimido não vira "estourou o limite": o motivo errado manda o operador olhar o lugar errado) · guards devolvidos batem com os fatos lidos.
→ Rodar: `-m unit` + `ruff check` + `lint-imports`.

### S2 · Migration — a fatia do E3 (1,5d)
`funnels` §5.4 · `scheduled_touches` §5.5 (índice `(status, due_at)`) · `suppression_list` §4.4 · `orders` §3.3 · `customers` §3.4 · `funnel_conversions` (**novo — D8**) · aditivos em `channels_accounts` (`meta_tier`, `tier_usage_24h`, `warmup_stage`, `warmup_started_at`, `daily_cap`, `risk_accepted_at`, `vault_secret_id` — a migration do E1 já os anotou como dívida do E3) · aditivos em `tenants` (`proactive_max_per_contact_24h` com o CHECK de D1, `attribution_window_hours`) · `contacts.customer_id` (o dicionário §4.1 prevê e a fatia do E1 omitiu). RLS + policies + suíte de vazamento **no mesmo PR** (disciplina do E0-07, decisão 13). Atualização do `core/dicionario-de-dados.md` no mesmo PR.
→ Testes (`db`/`rls`): vazamento nas 6 tabelas novas com as três credenciais · o CHECK do teto visto **vermelho** (5 → erro) · sabotagem: caminho do lojista afrouxando o teto → barrado pelo privilégio, não por educação.
→ Rodar: `supabase db reset` + `-m "unit or db"` + `-m pipeline`.

### S3 · O abandono vira cadência (1,5d)
`internal.start_funnel_run(...)` numa transação única, no molde do `apply_domain_event`: ocasião tem funil habilitado? → **todos** os toques em `scheduled_touches` conforme a cadência, com o **nº 1 já vencido** (`due_at = now()`) para o dispatcher do S4 pegá-lo no tique seguinte. **Nenhum toque vai direto para a outbox** (D11): a escada é a única porta. `apply_domain_event` passa a rotear para ela e ganha o desfecho **`no_funnel`**; o texto fixo do E1 morre aqui (D6). `max_touches` é teto do funil, **não** teto por contato — os limites por contato são da escada (RF-034).
→ Testes (`db` + `pipeline`, arquivos novos): cadência de N toques materializada com `due_at` derivado do relógio injetado · funil desligado → `no_funnel` e zero linhas na outbox · **os quatro desfechos do E1 continuam verdes** (é a asserção que autoriza a emenda da Lei 1).
→ Rodar: `-m "unit or db"` + `-m pipeline`.

### S4 · O disparo: dispatcher, escada e o CAS de gravação (2d)
Task de 1 min: `internal.claim_due_touches(worker, limit)` SECURITY DEFINER, no molde do `claim_outbox_batch` (ADR-11: nada de `SELECT` global) → `q_scheduled`. Handler de `q_scheduled`: transação curta carrega o snapshot → escada do S1 → `internal.dispatch_touch(...)` grava na outbox **com os guards no `WHERE`** (D2); guard falhou → `scheduled_touches.status='cancelled'` com `cancel_reason`, nada sai. Desfechos como dado, arquiva em todos (decisão 74).
**Três dívidas do S3 fecham aqui:** (a) **o fio ponta a ponta volta** — o cenário `pipeline` do E1 (`test_scenario_abandonment`) foi aposentado com o toque fixo, e desde o S3 nada chega ao canal; o S4 devolve a prova completa (evento → cadência → escada → outbox → sender → canal); (b) **`funnels.touches` ganha CHECK de forma** — hoje é `jsonb` livre, e uma cadência sem `delay` ou com `n` duplicado só explode em runtime às 3h da manhã; cadência vazia deixa de ser no-op silencioso; (c) o docstring de `Guards` no `ladder.py` cita uma coluna `is_proactive` que não existe — reconcilie com as fontes reais que o S2 fixou (`message_outbox.kind` para as 24h, `scheduled_touches.sent_at` para as 72h) e acrescente o teste `db` de que a janela contada é a que o módulo nomeia.
→ Testes (`pipeline` + `db`): toque vencido vira envio · **a corrida encenada de verdade**: mensagem nova entre a decisão e a gravação → nada sai · supressão bloqueia · limite de 24h bloqueia · sabotagem-coroa: tirar a escada do caminho → reprova só os testes dela.
→ Rodar: `-m pipeline` completa + `-m "unit or db"`.

### S5 · `order_paid`: cancela, e credita (1d)
Handler de domínio: upsert no espelho (`orders`/`customers`) → cancela os `scheduled_touches` pendentes do contato (`cancel_reason='stale_order_paid'`, o vocabulário da escada) → houve toque enviado dentro da janela de atribuição? → linha em `funnel_conversions` (D8). **Se o caminho pedido→contato (três saltos por texto: `orders.customer_external_id` → `customers.external_id` → `customers.phone_e164` → `contacts.phone_e164`) pesar, o conserto aditivo é `orders.contact_id`** — achado do S2, decidido aqui e não lá.
→ Testes (`db` + `pipeline`): cancelamento imediato · atribuição dentro e fora da janela, com o relógio injetado · **o pagamento chega a tempo sob rajada** — o cenário que a decisão 58 (expiração da crença) consertou ganha agora o teste de produto, não só o de fila.
→ Rodar: `-m "unit or db"` + `-m pipeline`.

### S6 · Supressão nas três vias (1d)
(a) **Botões Autorizar/Bloquear** em todo toque a contato novo; bloquear grava `suppression_list` (`explicit_block`), e **`contacts.opt_status` é projeção dela, nunca um segundo escritor** — a versão anterior desta linha mandava escrever a coluna *e* declarava a lista autoridade, que é exatamente a divergência que a decisão vinha consertar (contradição minha, apontada pelo S6). **O reconhecimento da resposta do botão é determinístico e mora no turno de entrada, entre a FASE 1 e a FASE 2** — decisão de consentimento nunca passa pelo modelo. (b) **Silêncio após 3 disparos em funis distintos** → supressão automática (`no_response_after_3`), contada a partir dos toques enviados, sem coluna nova. (c) **Opt-out por intenção** → tool `record_optout` (`intent_optout`): o modelo detecta, a tool executa e audita. Opt-out **suprime envio, não apaga dado** (RNF-044).
→ Testes (`unit` + `db` + `pipeline`): cada via · a lista é consultada antes de **todo** proativo · sabotagem: pular a checagem em um dos caminhos → reprova.
→ Rodar: `-m "unit or db"` + `-m pipeline`.

### S7 · Canais: Evolution com anti-ban, e o tier da Cloud (2d)
Adaptador `channels/evolution.py` (transporte injetável, no molde do `cloud_api.py` do E1). No **sender** (D10): jitter 30–120s por número (pelo `Randomness` injetável, que já tem trava de fitness), warm-up 20→50→100, teto duro 300/dia, aceite de risco (`risk_accepted_at` + `audit_log`). No **dispatch**: variação de copy por LLM com a regra "nunca repete a última" + **validador determinístico de copy no lugar do Judge** (D3), e `payload.generated` gravado no item. Cloud: token bucket por número no `meta_tier`; a 80% → **pausa proativos + alerta**; reativas nunca pausam (RF-034). **`funnels.channel_preference` ganha leitor aqui** — a coluna existe desde o S2 e hoje ninguém a lê (o roteamento escolhe "a conta ativa mais nova"); config sem consumidor é config que mente (achado do S4). **Mudança de lei no mesmo PR:** `CLAUDE.md` e arquitetura passam a dizer "toda resposta **reativa** passa pelo Judge 1".
→ Testes (`unit` + `db` + `pipeline`): cada limite isolado · copy repetida → reprova · o validador barrando número/prazo/link que a base não tinha, e o toque **não** saindo · sabotagem: proativo furando o teto diário → reprova · a 80% do tier o proativo para **e a resposta reativa continua saindo** (as duas metades no mesmo teste).
→ Rodar: `-m "unit or db"` + `-m pipeline`.

### S8 · Reconciliação por poll — o cinto de segurança do ADR-3 (1,5d)
Porta `connectors` + job de 15 min: cursor por conta → traduz para o formato do webhook → **mesma `ingest_webhook`** (D5) → `connector_accounts.sync_status`/`last_sync_at`. Segredo só por `get_connector_secret` (E0-22). **Dívida nomeada que fecha aqui:** `webhook_events` guarda `source`/`source_account_id` como texto e cada handler refaz o join para `connector_accounts` — são dois lugares hoje e três depois deste passo; o conserto aditivo é resolver `webhook_events.connector_account_id` na própria ingestão (achado do S5).
**Recomendação de escopo:** o **mecanismo** fecha no E3 contra um adaptador dublê; o **adaptador Shopify real** vai para o E4, onde o OAuth nasce — construí-lo aqui exige um token de loja dev colado à mão, que é justamente o que o E4 vem eliminar. Se o Bruno preferir tudo no E3, o passo cresce ~1d e depende da loja dev (B-4).
→ Testes (`db` + `pipeline` + 1 `contract` não bloqueante quando houver credencial): replay 3x pelos dois caminhos = 1 efeito · evento que só o poll viu entra igual · cursor não regride.
→ Rodar: `-m "unit or db"` + `-m pipeline`.

### S9 · As tools que o modelo escolhe + a ocasião no prompt (1,5d)
`get_order`, `get_tracking`, `escalate_to_human`, `record_optout` (§5) com o laço de escolha limitado — fecha a emenda E2 do plano anterior ("tool que o modelo ESCOLHE entra no E3, quando existir escolha"). `get_customer_context` ganha consumidor real (fecha a decisão 88b). Nasce a **biblioteca de prompts de ocasião de funil** (`pix_pending`, `checkout_abandoned`, `cart_abandoned`): o mecanismo de seleção por `origin_occasion` já existe desde o S4 do E2 — aqui nasce o conteúdo, ainda como fixture rotulada (D7 do E2 continua valendo; conteúdo com fonte no formulário é o gerador do E4).
→ Testes (`unit` + `db` + `pipeline`): recusa de tenant alheio em cada tool nova · laço com teto de idas · a ocasião certa seleciona a camada certa · sabotagem: tool confiando no `tenant_id` dos argumentos → reprova.
→ Rodar: `-m "unit or db"` + `-m pipeline`.

### S10 · Cenário 11, A5/A6 completas e a primeira carga (1,5d)
Cenário 11 do §3.3 inteiro (pagamento cancela · supressão bloqueia · evento obsoleto descartado · funil respeita limite/cadência/intervalo). A6 (~25 cenários de dispatch) e o que falta da A5 (outbox/sender). **Carga leve 10x** com o relatório dos critérios do §5 do plano de testes. **Duas travas novas que os passos pediram:** (i) **fitness function de "coluna sem escritor"** — uma varredura schema × código-fonte; `connector_accounts.sync_status`/`last_sync_at` ficaram dois marcos sem ninguém escrevendo, e só apareceram porque o S8 por acaso as tocou (sugestão do S8; é a irmã automatizada da regra *guarda sem alvo mente*); (ii) **quebrar `internal.apply_domain_event` em funções pequenas** — plpgsql não tem substituição parcial, então todo passo que mexe numa linha redigita 150, e no S7 uma transcrição da versão errada apagou em silêncio o roteamento do S5. Enquanto isso não existir, a regra é **extrair o corpo da migration viva, nunca redigitar**.

**Contrações e dívidas nomeadas que fecham aqui:** o shim `apply_domain_event(bigint, text)` da compatibilidade N-1 morre, num commit próprio; `scheduled_touches.conversation_id` vira `NOT NULL` (desde a D11 nenhum toque nasce sem conversa, e a escada não protege um toque que não tem uma); e o toque preso em `enqueued` — se o job morrer na DLQ, `claim_due_touches` nunca mais o pega — ganha **métrica de idade no S11**, não um segundo relógio (achados do S4). O shim morre aqui, num commit próprio — dívida com data marcada é dívida; sem data, alguém no E5 acha que existe um caminho de texto fixo para reusar.
→ Rodar: suíte inteira + `tests/load/`.

### S11 · Observabilidade do marco (0,5–1d, condicional a B-2/B-3)
Métricas: toques agendados/enviados/**cancelados por motivo** (é o motivo que diagnostica, não o total), conversões atribuídas, uso do tier, estágio de warm-up, profundidade da `q_scheduled` e sua DLQ. Alertas de tier a 80% e de `channels_accounts.status='banned'` com log + métrica + alerta no mesmo `trace_id`. PII nunca (nem telefone, nem conteúdo de toque).

**Executado em 2026-08-06, na metade que não depende de credencial** — mesmo tratamento do E1 e do E2: o que não precisa de rede fecha, o que precisa fica pendente e explícito.

*Fechado:*
- **Os alertas in-app.** O de tier a 80% **já existia desde o S7** (`internal.record_channel_send`, um por travessia, com teste de PII) — a premissa de que faltava estava errada. Entraram os três que faltavam de fato, numa varredura só (`internal.sweep_health_alerts`, tarefa periódica do processo, `worker_role`): **`touch_stuck`** (toque em `enqueued` além de 30 min — alerta de IDADE, e a migration não tem um único `UPDATE` em `scheduled_touches`, porque um segundo relógio poderia reenviar o que já saiu), **`channel_banned`** e **`connector_error`** (tipo que existia no CHECK desde o E2 e nunca teve escritor). Deduplicação por alerta ABERTO equivalente. Sem PII, com teste de vazamento em cada bloco.
- **`connector_accounts.sync_error_since`**, nova, com escritor no mesmo commit (`finish_sync`): "persistente" não cabia em `last_sync_at`, que avança inclusive nos passes que falham.
- **Quatro views de métrica** em `public`, `security_invoker = true`: `metrics_touches` (por desfecho e por motivo), `metrics_stuck_touches`, `metrics_conversions`, `metrics_channel_health`. RLS provada nos dois sentidos (lojista, worker, `anon`). `channels_accounts` ganha GRANT **por coluna** para `authenticated`, para não levar telefone e referência do Vault à Data API.
- **O `traceparent` atravessando as filas novas.** O slot `otel` existia desde o E1 e nunca teve produtor: `dispatch_pass` e `reconcile_pass` recebem uma `TraceSource`, e `internal.ingest_webhook` ganha `p_otel` (último parâmetro, com default; DROP e recreate, senão a chamada de cinco argumentos da Edge Function ficaria ambígua).

*Pendente de B-2/B-3, nomeado:* exportador OTLP, SDK do Logfire, compose com Alloy, redação de PII no processor, spans de custo a partir de `llm_calls`, profundidade de fila/DLQ como métrica, e o **cenário 14** (mesmo `trace_id` nos dois backends). A `TraceSource` é o ponto único onde tudo isso se pluga.

*Dívida nomeada, fora do escopo do passo:* `channels_accounts.status = 'banned'` **não tem escritor automático** — hoje quem marca é operação (e o hub, no E5/E6). A detecção pelo provedor é uma das perguntas que só a suíte `contract` da Evolution pode responder (ver `channels/evolution.py`). O alerta é o leitor; o escritor automático continua faltando.

### S12 · Provas de conclusão
1. **Simulação do dia real**, ponta a ponta, num tenant sintético: abandono → funil com cadência → contato responde → toques futuros morrem e a conversa vira suporte normal (E2) → pedido pago dentro da janela → conversão atribuída; em paralelo, um contato que bloqueia é suprimido e um pagamento cancela um funil ativo.
2. **A5 + A6 verdes**, cenário 11 verde, suíte completa verde.
3. **Carga 10x** dentro dos critérios.
4. Fechamento em `docs/estado-da-execucao.md` com decisões numeradas.

---

## 8. Mudanças exigidas nos docs canônicos

Os docs são a fonte da verdade — cada item entra no **mesmo PR** do passo que o usa:

| Doc | Mudança | Passo |
|---|---|---|
| `dicionario-de-dados.md` | `funnel_conversions` (D8) · `tenants.proactive_max_per_contact_24h` e `attribution_window_hours` (D1/D8) · `contacts.customer_id` (omissão do E1) · nota de integridade nº 7: o teto de 4 é CHECK, não convenção · **`scheduled_touches.cancel_reason` passa a carregar o vocabulário da escada** — os quatro valores do dicionário (`replied \| paid \| suppressed \| manual`) não cabem os nove motivos, e achatá-los apagaria exatamente a métrica "cancelados **por motivo**" que o S11 pede (achado do S1) | S2 |
| `requisitos-e-entidades.md` **RF-032** | registrar que a escada checa staleness sempre, e por quê (D12) | S2 |
| `arquitetura §5.2` | o fluxo ainda diz "primeiro toque via outbox, próximos em `scheduled_touches`" — exatamente o que a D11 derrubou. Corrigir **junto com o código que a torna verdadeira** (achado do S2) | S3 |
| `dicionario-de-dados.md` §4.1/§4.4 | quem é a autoridade sobre "este contato está bloqueado": `suppression_list` manda, `contacts.opt_status` vira projeção. Dois lugares respondendo o mesmo fato divergem no pior dia (achado do S2) | S6 |
| `arquitetura §5.4` | registrar onde o teto é materializado e quem pode afrouxá-lo | S2 |
| `ordem-de-execucao.md §E3` | estimativa 7–10 → 17 dias (escopo completo), com o porquê (§9) | S0 |
| `CLAUDE.md` (invariantes) + `arquitetura §5.5` + `requisitos RF-015` | "toda resposta passa pelo Judge 1" → **"toda resposta reativa"**; disparo e campanha respondem ao validador determinístico de copy (D3) | S7 |
| `dicionario-de-dados.md` | `connector_accounts.sync_error_since` (§3.2) · os três tipos novos de `alerts` e **a tabela de quem escreve cada tipo** — a lista existe porque tipo de alerta sem escritor é proteção decorativa, e seis dos doze continuam sem um · §6.6 com as quatro views de métrica · o `otel` nos payloads de fila (§5.2) | S11 |

## 9. Por que a estimativa cresce (honestidade sobre o calendário)

O `ordem-de-execucao` v2.0 diz 7–10 dias para o E3. A soma dos passos dá **17** (ou ~15 movendo o adaptador Shopify para o E4). A diferença não é inflação: o texto de uma linha do marco ("Evolution + anti-ban", "reconciliação") esconde **um adaptador de canal inteiro**, **uma porta de conector nova** e **o espelho de pedidos e clientes** — três coisas que o E1 e o E2 nunca precisaram e que nenhuma estimativa anterior custou. Os três caminhos:

- **E3 completo (17d)** — tudo acima.
- **E3 recomendado (~15d)** — S8 fecha só o mecanismo; adaptador Shopify real migra para o E4, junto do OAuth.
- **E3 mínimo (~11–12d)** — S8 e S9 inteiros vão para o E4. O marco ainda entrega funil, proteções, pagamento, supressão e os dois canais; perde as tools de pedido (que ficam sem consumidor por mais um marco) e o cinto de segurança do ADR-3.

Recomendação deste plano era **o do meio**. **O Bruno escolheu o completo (2026-08-06):** o S8 entrega o adaptador Shopify real. Consequência aceita e registrada: as partes do S8 que tocam a rede (OAuth de loja dev, registro de webhook, poll real) ficam **pendentes e explícitas** até as credenciais do B-4 chegarem — o adaptador nasce contra transporte injetável e cassete, exatamente como o adaptador da Cloud API do E1, que está pronto há dois marcos esperando só um token.

## 10. Riscos

| # | Risco | Mitigação |
|---|---|---|
| R1 | **Banir o número de teste da Evolution** durante o desenvolvimento do S7 | Nenhum teste bloqueante toca a rede (regra do `CLAUDE.md`); o adaptador nasce contra transporte falso; a instância real só na `contract` sob demanda, já com warm-up ligado |
| R2 | **A escada tem TOCTOU por natureza** — decidir e gravar são momentos diferentes | É o D2 inteiro: o CAS de gravação revalida os guards. E o teste do S4 encena a corrida de verdade, não a simula |
| R3 | Custo de LLM da variação de copy por toque | Só na Evolution (Cloud usa template aprovado); medido em `llm_calls` desde o S5 do E2; se doer, a variação vira pool pré-gerado por funil com a mesma regra de não repetir |
| R4 | O espelho de pedidos crescer para um sync completo de plataforma | O E3 espelha **o que os eventos trazem** (`order_paid` e o poll de reconciliação). Sync completo de catálogo/pedidos é RF-070/RF-042 e tem dono: E5 |
| R5 | Cadência de funil virar linguagem de programação em `jsonb` | `touches` é lista de `{n, delay, template_ref/copy_base, cta}` e nada mais. Condicional dentro do funil é sinal de que a regra queria ser código |
| R6 | A prova do S12 depender de credenciais que não chegaram | Mesmo tratamento do E1 e do E2: tudo fecha contra dublê/cassete e as provas dependentes ficam **pendentes e explícitas**, nunca silenciosamente puladas |

## 11. O que depende do Bruno

| Item | Bloqueia | Quando |
|---|---|---|
| ~~Confirmar D1, D3 e D8; escolher o escopo~~ | — | **respondido em 2026-08-06**: D1 sim · D3 **só o tempo real passa pelo Judge** · D8 sim · escopo **completo** |
| **Instância Evolution + número** (parte do B-4) | a `contract` do S7 e a prova 1 do S12 | quando puder — o código fecha sem ela |
| **Loja dev Shopify + credenciais OAuth** (parte do B-4) | a `contract` do S8 e o poll real | quando puder — o adaptador fecha contra cassete |
| Logfire + Grafana (B-2/B-3) | S11 | quando existir |
| Pendências herdadas do E2: chave do OpenRouter na máquina nova · token Meta + `phone_number_id` | S11/S12 do E2, e o S7 daqui | quando puder |

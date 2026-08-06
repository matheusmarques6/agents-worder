# Plano E2 — Agente real, eval-first

**Marco:** E2 · **Duração estimada:** 10–14 dias úteis · **Pré-requisito:** E1 fechado em código (feito — decisão 74)

Fontes canônicas: `core/arquitetura-plataforma-agentes-whatsapp.md` (§3 contratos, fluxo 5, ADR-12) · `core/dicionario-de-dados.md` §2.1/2.4/2.5/§6.1–6.5 · `core/testes-e-cicd.md` §2 e §5 · `core/ordem-de-execucao.md` §E2. Este documento é o S0 do plano v2 revisado pelo Bruno (2026-08-03); o que aqui diverge do texto dele está marcado como decisão.

---

## 1. O que é o marco

O E1 provou o motor com resposta fixa; o E2 troca a resposta fixa pelo **agente real** — LLM, prompt em camadas, tools, conhecimento e Judge 1 — **sem tocar no motor**. A ordem interna é eval-first: rubricas e pack ANTES de qualquer código de agente, porque o gate de ativação de uma versão é o pack passando, não uma opinião.

## 2. As duas leis (vigentes em todos os passos)

1. **Proibido mudar testes existentes.** O agente real é uma implementação nova da costura `respond` que o E1 deixou pronta. Motor e os 28+ cenários ficam intocados; todo teste novo é arquivo novo. Protocolo de exceção: o passo que parecer exigir tocar teste antigo **para** e escala com um teste novo vermelho na mão — nunca edição.
2. **Rodar testes é obrigatório em cada passo** — os comandos exatos estão em cada S; verdes antes do PR; `-m pipeline` local sempre que o runtime é tocado.

## 3. Decisões fixadas

| # | Decisão | Estado |
|---|---|---|
| **D1** | **LLM (emendado pelo Bruno em 2026-08-03):** o modelo do agente é **config por tenant** — campo `model` na config de `agent_versions`, default `claude-sonnet-5`, editável no admin (tela no E6; o dado nasce no S2). **Judge 1 é fixo da plataforma — `claude-haiku-4-5` — nunca por tenant**: portão de segurança não enfraquece por config de cliente. Limite de regeneração do Judge pré-envio: **2** (nos defaults canônicos do CLAUDE.md). **Todo tráfego LLM — chat e embedding — via OpenRouter**: um adapter OpenAI-compatível, modelo como string | **fixado (decisão 79)** |
| **D2** | **Embedding:** `text-embedding-3-small` via OpenRouter, **dimensão 1536** — barato, multilíngue sólido em PT-BR, e a dimensão congela na migration do S2 (expand-contract torna troca cara: decidir ANTES do S2 é a razão de o D2 existir) | **fixado (decisão 79) — S2 desbloqueado** |
| **D3** | **Limite do pack é POR RUBRICA** (testes-e-cicd §5), não agregado; **zero `critical` inegociável em todas**. Piso por rubrica: **≥ 0,85** — os arquivos do S1 já nasceram com esse valor, nada muda | **fixado (decisão 79)** |
| **D4** | **B-2 (Logfire):** sem ela, a prova de custo/latência e o cenário 14 ficam **pendentes e explícitos** (mesmo tratamento do B-4 no E1) — nenhum outro passo bloqueia | fixado |
| **D5** | **A assinatura do §3 vive DENTRO do responder, não na costura do motor.** A costura real do E1 é `respond(job: InboundJob) → dict` (`worker.py`), protegida pela Lei 1. A fábrica do `AGENTS_RESPONDER` devolve um callable com a forma do motor que, por dentro, carrega conversa + mensagens pendentes (transações curtas próprias, `SET LOCAL` — a mesma regra das tools do S7) e chama o núcleo com a forma do §3 (`respond(conversation, pending_msgs) → draft`). O S4 implementa o núcleo; o S9 implementa o adaptador | fixado |
| **D6** | **Fonte de verdade da versão ativa é o índice parcial** em `agent_versions` (um único `active` por tenant). `tenants.active_version_id` **não entra** no S2 — dois mecanismos dizendo a mesma coisa divergem no pior dia; se uma FK-cache se provar necessária (leitura quente no hub), entra depois como expand com constraint que a escravize ao índice | fixado |
| **D7** | **A config do agente-piloto do E2 é fixture de teste declarada, não produto.** A lei "nenhum conteúdo de prompt sem fonte rastreável no formulário" rege o GERADOR do E4; o piloto do E2 existe para provar o mecanismo de camadas e é rotulado como tal | fixado (correção do próprio plano v2) |

## 4. Toolset mínimo do E2

`search_knowledge` e `get_customer_context` — só. Tools de pedidos chegam no E3 com as tabelas de pedidos. Toda tool: valida tenant + autorização ela mesma (mensagem de contato é entrada hostil; o modelo nunca decide acesso), transação curta própria com `SET LOCAL`, execução gravada em `tool_calls`.

## 5. Escopo negativo do E2

Funis e proativos = E3 · gerador de prompt do formulário = E4 · flywheel/uso secundário = E6 **sob ADR-12 (hoje: suspenso)** · UI = E5. Um PR que traga qualquer um desses volta.

---

## 6. Os passos

Cada S fecha com os comandos listados verdes e vira PR pelo gate (branch → PR → 4 checks → merge). Vermelho primeiro em tudo que especifica comportamento.

### S1 · Rubricas + pack base — antes de qualquer código de agente (1,5d)
Rubricas versionadas no repo: correção factual sobre o conhecimento · tom/idioma/never_say_ai · segurança (injeção via contato, revelar prompt, forçar "sou uma IA") · limites de escopo. **Cada rubrica e cenário cita o RF de `requisitos-e-entidades.md` que valida** — rastreabilidade é regra, não cortesia. **ADR-12: cenários 100% sintéticos** — copiar conversa real para o pack é exatamente a cópia que a suspensão proíbe.
→ Testes (`unit`): parser da rubrica · pontuação → `pass|fail|critical` · pack com rastreabilidade conferida (cenário sem RF → o teste reprova).
→ Rodar: `pytest -m unit` + `ruff check`.

### S2 · Migration — a fatia do E2 (1d)
`agent_versions` §2.1 (append-only; só `status`/`activated_at` mutam; **índice parcial: um único `active` por tenant — D6, sem a FK**; a config carrega o campo **`model`** — D1 emendado) · `knowledge_chunks` §2.5 (**`vector(1536)`** por D2; nota LGPD do dicionário: chunk de FAQ fora da purga por contato, derivado de conversa dentro) · `eval_runs`/`judge_scores`/`tool_calls`/`llm_calls` §6.1–6.4 (**`llm_calls` grava `provider` + `model`**: o roteamento é OpenRouter e o modelo varia por tenant) · **`alerts` §6.5** (o S8 escreve nela) · `scenarios` §2.4 **nasce interna** — o gate duplo do E4 (lojista testa cenários) sugere exposição futura; expor é mudança aditiva (grant + policy), o caminho expand. RLS + policies + suíte de vazamento no mesmo PR (disciplina do E0-07).
→ Testes (`db`/`rls`): vazamento nas tabelas novas com as três credenciais · índice parcial visto **vermelho** (segunda ativa → erro) · sabotagem no ritual.
→ Rodar: `supabase db reset` + `-m "unit or db"` + `-m pipeline`.

### S3 · Harness de evals com LLM dublê (1d)
Runner: pack + rubrica → executa contra um responder → `eval_runs`/`judge_scores` → agrega **por rubrica** contra D3. É **gate de ativação, não CI** (§5). Nasce contra responder roteirizado.
→ Testes (`unit`+`db`): agregação por rubrica · persistência · dublê ruim reprova, dublê bom aprova.
→ Rodar: `-m "unit or db"`.

### S4 · agent_core — prompt em camadas, puro (1,5d)
**Think-gate definido (decisão 81, com o Bruno em 2026-08-03):** é o **portão do raciocínio caro** — decide, ANTES de chamar o LLM, se a entrada pendente merece raciocínio estendido. Puro e determinístico, devolve decisão **e motivo como dado** (`friction | many_messages | long_message | multiple_questions | simple_input | nothing_from_the_contact`), porque "por que esta conversa pagou o caminho caro" é pergunta que o S10 faz. Precedência: atrito primeiro (reclamação curta continua sendo reclamação). Nenhum doc canônico definia o gate — arquitetura §3 e testes-e-cicd §2 só o nomeavam.

Ordem literal: base → cenário por ocasião → contexto do cliente → tools → conhecimento. Seleção por `origin_occasion` — no E2 a ocasião é support/inbound; o mecanismo nasce completo, a biblioteca de ocasiões de funil chega no E3. Injeção de contexto (total de compras, ticket médio, primeira compra) · adaptação de idioma · never_say_ai · think-gate. Config = linha de `agent_versions` passada como valor. Núcleo na forma do §3 (`respond(conversation, pending_msgs) → draft`) — o adaptador para a costura do motor é o S9 (D5).
→ Testes (`unit`): um por regra da tabela §2, vermelhos primeiro · ordem das camadas como asserção literal.
→ Rodar: `-m unit` + `lint-imports`.

### S5 · Porta do LLM + llm_calls (1d)
Porta injetável; adapter de D1 (**OpenAI-compatível apontando para OpenRouter — modelo é string, vem da config do tenant**); toda chamada grava `llm_calls` (tokens, latência, custo, `provider`, `model`). **Lei de PII explícita:** conteúdo de prompt/resposta fica SÓ no Postgres; para telemetria (S10) sobem custo/latência/ids — nunca conteúdo. Cassette só no `contract` semanal. **Trava de rede no gate: teste de fitness pytest** (o padrão das travas de AST do relógio/acaso — vive na suíte `unit`, sabotagem prova o raio), não grep de CI: host do provedor fora de `tests/contract` reprova. Com D1 emendado a allowlist tem **um host só — `openrouter.ai`** —, permitido apenas em `tests/contract`.
→ Testes (`unit`+`db`+1 `contract` não-bloqueante): contrato da porta · persistência · classificação de erro (reusa a unidade 4).
→ Rodar: `-m "unit or db"`.

### S6 · Conhecimento (1d)
Embedder injetável; ingestão de chunks (fonte manual no E2); recuperação por similaridade na camada de repositório.
→ Testes (`db`): ingestão/recuperação com embedder determinístico · RLS · sem rede.
→ Rodar: `-m "unit or db"`.

### S7 · Tools com autovalidação (1d)
`search_knowledge` + `get_customer_context` (§4). Cada consulta: transação curta própria com `SET LOCAL` do tenant, dentro da FASE 2 mas **nunca uma transação atravessando o LLM** — ADR-6 vale dentro do responder também. Toda execução grava `tool_calls`.
→ Testes (`unit`+`db`): recusa de tenant alheio · registro · sabotagem: validação removida → só o teste dela reprova.
→ Rodar: `-m "unit or db"`.

### S8 · Judge 1 — pré-envio, dentro do responder (1,5d)
Fluxo da arquitetura (fluxo 5): **pré-envio** (`pre_send`): reprova → regenera (limite de D1); esgotou → **não envia** + linha em `alerts` (revisão humana); `critical` no pré-envio → nunca sai, alerta imediato. **Auto-correção é do pós-envio e pertence ao S9** — este passo não a implementa. Integrado dentro do responder — motor intocado.
→ Testes (`unit` + cenário `pipeline` novo, arquivo novo): "resposta reprovada nunca alcança a outbox" · **sabotagem-coroa do marco:** remover o Judge do caminho → só esse cenário reprova. A lei "100% passa pelo Judge 1, sem exceção nem em teste de carga" virando asserção.
→ Rodar: `-m "unit or db"` + `-m pipeline` completa.

### S9 · Responder real na composição + shadow + pós-envio (1,5d)
`AGENTS_RESPONDER` (fábrica `module:callable`, morte alta se malformada — doutrina existente) devolvendo o adaptador de D5. **Shadow (`shadow_until`): avalia, nunca retém envio** — 100% das respostas enfileiram em `q_evals`; fora do shadow, amostragem. Consumidor de `q_evals` no molde do handler de `q_domain_events` (decisão 74: desfechos como dado, arquiva em todos, exceção = bug): roda `post_hoc` → `judge_scores`. `critical` pós-envio → auto-correção + alerta; **a correção é um outbound normal via outbox e passa pelo Judge 1 como qualquer resposta** — a lei dos 100% não tem porta lateral.
→ Testes (`pipeline`, arquivos novos): eval não bloqueia envio · shadow = 100% / fora = amostragem · correção passando pelo Judge · sabotagem: auto-correção pulando o Judge → reprova.
→ Rodar: `-m pipeline` completa + `-m "unit or db"`.

### S10 · T4 intercalada, se B-1/B-2/B-3 (1d, condicional)
E0-19/20/21; spans de custo/latência a partir de `llm_calls` (conteúdo nunca — a lei de PII de novo); cenário 14 (`traceparent` no slot `otel` atravessando as filas; log+métrica+alerta com o mesmo `trace_id`).

### S11 · Pack contra o LLM real — gate de ativação (1d)
Rede só aqui, sob demanda. Itera prompt/rubrica/retrieval até **cada rubrica ≥ D3 e zero `critical`**. Runs persistidos; primeira versão aprovada → `active` no tenant de teste (o índice parcial de D6 vigia).
→ Rodar: harness + `-m "unit or db"` + `-m pipeline`.

### S12 · Prova de conclusão
Conversa real no número de teste (← adaptador E1 + **B-4**) · pack ≥ D3 por rubrica, zero `critical` · custo/latência no Logfire (← **B-2**) · cenário 14 verde · fechamento no estado.

---

## 6.1 Emendas da execução (o que mudou entre o plano e o feito)

Escrito enquanto o marco era executado, para que o plano não minta sobre o
código. Cada linha tem a decisão numerada em `docs/estado-da-execucao.md`.

| # | O plano dizia | O que foi feito, e por quê |
|---|---|---|
| E1 | S9 num passo só | **S9 dividido em S9a e S9b** (precedente do PR 2a/2b do E1): a composição do responder sozinha passa de 800 linhas, e juntá-la ao pós-envio faria um PR que ninguém revisa de verdade |
| E2 | "laço de tools limitado" no S9 | **No E2 a recuperação é determinística, não escolhida pelo modelo** (decisão 88a). As duas tools do marco são contexto **incondicional**, e uma ida e volta a mais custa segundos numa conversa de WhatsApp. Tool que o modelo ESCOLHE entra no E3, quando existir escolha (pedido por id, rastreio, chamar humano). O rastro em `tool_calls` é idêntico — quem executa continua sendo o `run_tool` do S7 |
| E3 | `get_customer_context` no toolset do E2 | **Construída e testada, sem consumidor no E2** (decisão 88b): a camada `customer_context` do RF-010 fala de PEDIDOS e o espelho chega no E3. Devolver zero seria inventar dado |
| E4 | "reprovação regenera" (RF-015), sem dizer o que reprova | **Regra por severidade fixada com o Bruno** (decisão 87): `critical` nunca envia e nem regenera · só `standard` regenera com os critérios falhos como feedback · esgotou → **envia a melhor versão** · juiz ilegível conta como reprovação, e rascunho que nenhuma tentativa conseguiu julgar não sai. **O piso 0,85 do D3 não se aplica por mensagem** — ele é do gate de ativação, sobre um pacote |
| E5 | nada sobre o não-envio no banco | `internal.conclude_turn` ganhou o **ramo de conteúdo nulo** por `create or replace` com a mesma assinatura (aditivo, N-1 compatível). Sem ele, bloquear uma resposta prenderia a conversa e o coalescer recriaria o job para sempre |
| E6 | segredo do provedor sem lugar definido | **Chave do OpenRouter é segredo de plataforma** → env, não Vault (decisão 84c). Vault é para segredo **por tenant** (token Meta, E4), e é lá que o E0-22 vence |
| E7 | S9: "100% das respostas enfileiram em `q_evals`", sem dizer **quem** enfileira | **O `conclude_turn` enfileira, atômico com a inserção na outbox** (decisão 91). O responder não pode: ele roda na FASE 2, antes do CAS, e um rascunho descartado por mensagem nova seria avaliado sem nunca ter sido enviado — com `critical`, a auto-correção mandaria ao cliente a correção de uma mensagem que não existiu para ele. O worker depois do commit tocaria o motor (Lei 1) e abriria janela de perda |
| E8 | S9: "fora do shadow, amostragem" | **Medidor de perigo determinístico, sem sorteio** (decisão 92, redesenhada pelo Bruno). Sorteio trata "obrigado" e "seu reembolso de R$ 340 cai em 3 dias" como igualmente arriscados; auditoria segue o risco. O medidor é o irmão do think-gate do S4 — puro, motivo como dado — e tira o `Randomness` do desenho. O ponto cego (afirmação falsa sem número nem promessa) se fecha por **lote de calibração sob demanda** sobre o que ele descartou, no molde do S11, não por amostragem contínua |

## 7. Riscos

| # | Risco | Mitigação |
|---|---|---|
| R1 | B-4/B-2 atrasarem e travarem S11/S12 | Mesmo tratamento do E1: tudo fecha contra dublê/cassete, as provas dependentes ficam pendentes e explícitas |
| R2 | Custo do S11 (iteração contra LLM real) | Runs persistidos em `eval_runs` — iteração retoma de onde parou; pack roda sob demanda, nunca no gate |
| R3 | O prompt em camadas crescer acoplado ao piloto | D7: o piloto é fixture rotulada; o teste da ordem das camadas é literal e não conhece conteúdo |
| R4 | Regeneração do Judge estourar a lease da conversa | O keepalive do E1 já renova a cada 45s durante trabalho longo; o limite de D1 (2 regenerações) põe teto no pior caso |

## 8. O que depende do Bruno

| Item | Bloqueia | Quando |
|---|---|---|
| ~~Confirmar D1, D2 e D3~~ | — | **feito em 2026-08-03 — decisão 79** |
| ~~**Chave do OpenRouter**~~ | — | **entregue em 2026-08-03**: a suíte `contract` rodou e confirmou `usage.cost` e as 1536 dimensões. **Precisa ser recolocada na máquina nova** — ver "Retomada em OUTRA máquina" no estado |
| **B-4** token System User + número de teste (checklist da decisão 73, ~15 min) | prova 1 do E1 **e** S12 do E2 | quando puder |
| **B-2** Logfire | S10 e a prova de custo/latência do S12 | quando existir |

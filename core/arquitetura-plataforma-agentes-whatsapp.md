# Arquitetura — Plataforma de Agentes WhatsApp para E-commerce

**Versão:** 1.3 · **Data:** 2026-08-01 · **Autor:** Bruno + Claude (descoberta + 3 rodadas de revisão técnica)
**Status:** Aprovada para desenvolvimento.
**Changelog v1.3:** CAS da conclusão passa a validar `processing_generation` e `next_inbound_seq = target_seq` (mensagem chegando durante o LLM invalida o draft); dedup de job formalizada como validação no worker (chave determinística não deduplica no pgmq); todo acesso cross-tenant vira função de claim SECURITY DEFINER (`claim_outbox_batch` etc.) — sem SELECT geral para roles da aplicação; reconciliação Meta só por status webhook (consulta direta é hipótese a validar); NFR de webhook reescrito de forma verificável; unicidade de evento inclui `source_account_id`; PII: default é NÃO coletar CPF/nascimento no e-commerce, senão envelope encryption.
**Changelog v1.2:** eliminado o duplo enfileiramento do inbound (só o coalescer cria job, atomicamente, com `processing_generation`); advisory lock transacional substituído por lease + compare-and-set (LLM fora de transação); `messages.seq` por contadores atômicos; garantia de envio rebaixada para "evitar perda e minimizar duplicidade" (idempotência da Cloud API não comprovada); prioridade estrita substituída por weighted polling + promoção por idade; semáforo por tenant assumido como processo asyncio único no MVP; segurança por role separado (worker/sender) + segredos via funções escopadas; runbook de restore com dois caminhos (Vault); retenção para treino suspensa até ADR de uso secundário (LGPD).

---

## 0. Resumo executivo

SaaS B2B multi-tenant que recupera vendas perdidas (carrinho, checkout, PIX) e faz atendimento completo via WhatsApp com agente de IA, para lojas Shopify, Nuvemshop e Yampi.

**Estilo arquitetural: monólito modular + workers assíncronos, config-driven.** Sem microserviços. Três planos de execução:

1. **Hub (Next.js/Vercel)** — formulário de onboarding, hub do lojista, admin do Bruno.
2. **Ingestão (Supabase Edge Functions)** — recebe todos os webhooks, valida, persiste **numa única transação SQL**, responde 200 em ms. Alta disponibilidade, fora da VPS.
3. **Runtime (serviço Python multi-tenant, Docker na VPS)** — **um processo asyncio único** no MVP: workers, coalescer, scheduler e senders são tasks do mesmo processo. Orquestra LLM + tools + judges, grava intenções de envio na outbox; senders entregam. Pode cair sem perder nada: eventos acumulam e são drenados na volta.

**Garantia de eventos (verificável):** nenhum evento é perdido **após a confirmação da ingestão**; eventos ausentes são reconciliados **quando a API de origem permite** (abandonos/pedidos: sim, por poll; mensagem inbound de WhatsApp cujo webhook nunca chegou: irrecuperável — não há replay). A entrega do webhook pelo provedor não é garantível por nós.

**Fonte de verdade única: PostgreSQL (Supabase)** — dados, config versionada, conversas, filas (pgmq), outbox, vetores (pgvector), evals. Nenhum banco adicional no MVP.

Dimensionamento validado: baseline ~1.500–3.500 eventos/dia (≈0,04/s); pico BF com rajada intra-hora de 20–50x ≈ 2–5 eventos/s. Postgres + pgmq operam isso com folga; o gargalo real é a latência da LLM e o throughput da Meta por número — por isso a arquitetura é fila + concorrência controlada + **transações sempre curtas** (nenhuma transação aberta atravessa chamada de LLM ou de API externa).

---

## 1. Decisões arquiteturais (ADRs)

### ADR-1 — Monólito modular + workers; não microserviços
- **Contexto:** dev solo (Bruno + Claude Code), ~25 tenants em 6 meses, domínio único (e-commerce).
- **Decisão:** um codebase Python (runtime) + um codebase Next.js (hub), fronteiras de módulo internas rígidas (§3).
- **Rejeitados:** microserviços (custo operacional injustificável para 1 dev); container por cliente (a diferença entre tenants é só config).
- **Consequências:** deploy único atualiza todos; diferenças por tenant vivem no banco, nunca no código.

### ADR-2 — Postgres único (Supabase) para tudo: dados, filas e vetores
- **Decisão:** Supabase Postgres como fonte de verdade. Filas = **pgmq**. Busca semântica = **pgvector**. Realtime do hub = Supabase Realtime.
- **Por quê:** fila no mesmo domínio de durabilidade dos dados; atomicidade "gravei E enfileirei"; zero vendors novos; volume muito abaixo do limite do Postgres.
- **Rejeitados no MVP:** Redis, Kafka, SQS, banco vetorial dedicado.
- **Gatilhos de revisão:** fila sustentada > ~50 msg/s ou lock contention → Redis/SQS; analytics pesando no primário → read replica.

### ADR-3 — Ingestão serverless, atômica e ciente da classe do evento
- **Decisão:** todo webhook entra por **Supabase Edge Function** que valida assinatura e faz **uma única chamada** a `ingest_webhook(p_source, p_source_account_id, p_external_event_id, p_payload)` (o tenant é resolvido pela conta de origem), que na mesma transação:
  1. `INSERT INTO webhook_events ... ON CONFLICT (source, source_account_id, external_event_id) DO NOTHING`; se duplicado, retorna `duplicate` e para. A unicidade inclui a conta de origem (que determina o tenant): plataformas com IDs sequenciais por loja (Nuvemshop, Yampi) podem repetir o mesmo `external_event_id` entre lojas diferentes — sem `source_account_id`, uma loja mascararia eventos da outra;
  2. **se evento de plataforma/canal (abandono, pagamento, status):** `pgmq.send('q_domain_events')`;
  3. **se mensagem inbound de contato:** grava em `messages` com `seq` atribuído atomicamente (ADR-6b) e atualiza `conversations.pending_response_at = now() + 10s`. **NÃO enfileira** — o job de processamento é responsabilidade exclusiva do coalescer (ADR-7). Isso elimina o duplo enfileiramento (job por mensagem + job do debounce), processamentos vazios e chamadas duplicadas de LLM.
- **Regra:** VPS pode ser SPOF do **processamento** (evento acumula); **nunca** da **ingestão** (perda seria permanente e silenciosa).
- **Cinto de segurança:** reconciliação por poll (15 min) chama a mesma `ingest_webhook()` — idempotência idêntica nos dois caminhos.

### ADR-4 — Consumo por weighted polling + promoção por idade (substitui prioridade estrita)
- **Contexto:** "inbound sempre primeiro" em modo estrito permite starvation: carga inbound contínua atrasaria indefinidamente eventos de pagamento (que **cancelam funis** — segurança do produto), abandonos e toques.
- **Decisão:** quatro filas pgmq consumidas por **weighted polling** — a cada ciclo, o consumidor tenta ler na proporção **8 `q_inbound` : 4 `q_domain_events` : 2 `q_scheduled` : 1 `q_evals`** (slots ociosos são emprestados à fila mais prioritária com trabalho pendente).
- **Promoção por idade:** `q_domain_events` com idade > 2 min é tratada com peso de inbound; `q_scheduled` com idade > 10 min sobe um nível. Latência inbound continua baixa, starvation fica impossível.
- **Concorrência por tenant:** semáforo de 3 processamentos simultâneos/tenant. **Premissa explícita do MVP: o runtime é um único processo asyncio** — todos os workers são tasks do mesmo processo, então o semáforo em memória é correto. Multi-processo/2ª VPS **exige** migrar o semáforo para lease no Postgres (`tenant_slots`: tenant_id, slot, lease_owner, lease_until) ou Redis — isso está amarrado ao gatilho de escala (§9). Mensagem de tenant no teto → `set_vt(+5s)`.

### ADR-5 — Semântica de fila: retry, visibility timeout e DLQ
- **Ciclo de vida de toda mensagem:**

  ```
  read(vt=60s)
    → processou ok        → archive()
    → falha transitória   → set_vt(backoff exponencial + jitter: 30s, 2min, 8min...)
    → falha permanente    → move para {fila}_dlq + archive()
    → read_ct > limite    → move para {fila}_dlq + archive()
  ```

- **Classificação de erros:** transitório (timeout LLM/API, 429, 5xx, deadlock) vs. permanente (payload inválido, tenant inexistente, credencial revogada). Limite de tentativas: 5 inbound/domain, 3 scheduled, 2 evals.
- **VT renovável:** processamentos longos renovam via heartbeat (`set_vt(+60s)` a cada 45s).
- **DLQs:** uma por fila; mensagem em DLQ dispara alerta; reprocessamento manual com um clique.

### ADR-6 — Exclusão mútua por conversa via lease + compare-and-set (substitui advisory lock transacional)
- **Contexto:** advisory lock transacional manteria transação aberta durante LLM + tools + judge (segundos a minutos): conexão presa, row versions vivas, pressão no pool e no autovacuum. Inaceitável — transações precisam ser curtas.
- **Decisão — três fases, transações curtas, LLM fora de qualquer transação:**

  ```sql
  -- FASE 1 · claim (transação curta, commit imediato)
  UPDATE conversations
     SET processing_token = :token,
         processing_until = now() + interval '2 minutes'
   WHERE id = :conversation_id
     AND (processing_until IS NULL OR processing_until < now())
  RETURNING last_processed_seq, version;
  -- sem linha retornada = outra task processando → desiste (set_vt curto)

  -- FASE 2 · trabalho (fora de transação)
  -- carrega contexto, chama LLM/tools/judge; heartbeat da lease se demorar

  -- FASE 3 · conclusão (transação curta, compare-and-set)
  UPDATE conversations
     SET last_processed_seq = :target_seq,
         processing_token = NULL, processing_until = NULL,
         version = version + 1
   WHERE id = :conversation_id
     AND processing_token = :token
     AND version = :expected_version
     AND processing_generation = :generation      -- job ainda é o corrente
     AND next_inbound_seq = :target_seq;          -- NENHUMA mensagem nova chegou
  -- na MESMA transação: INSERT message outbound + INSERT message_outbox + UPDATE slots
  -- CAS falhou → DESCARTA o draft, nunca envia; libera a lease apenas se o token
  -- ainda for o dono (UPDATE ... SET processing_token=NULL, processing_until=NULL
  --                    WHERE id=:conversation_id AND processing_token=:token);
  -- o novo debounce da mensagem recém-chegada gera outro job com o contexto completo
  ```

- **Invariante central:** mensagem chegando **durante** a geração do LLM invalida o draft. A ingestão incrementa `next_inbound_seq`; como o CAS exige `next_inbound_seq = :target_seq` (snapshot tirado pelo coalescer), a resposta desatualizada nunca sai — o job novo responde ao conjunto completo.

- **6b — Geração de `seq` sem corrida:** proibido `SELECT max(seq)+1`. A conversa mantém contadores `next_inbound_seq` / `next_outbound_seq`; atribuição por `UPDATE conversations SET next_inbound_seq = next_inbound_seq + 1 ... RETURNING`, dentro de `ingest_webhook()` (inbound) ou da FASE 3 (outbound). Constraint: `UNIQUE (conversation_id, direction, seq)`.

### ADR-7 — Debounce por coalescência com geração (job único garantido)
- **Contexto:** `FOR UPDATE SKIP LOCKED` evita dois coalescers na mesma linha, mas "limpou `pending_response_at` e caiu antes do send" deixaria a conversa órfã de job.
- **Decisão:** o coalescer (tick 2s) executa **numa única transação**: seleciona conversas vencidas (`FOR UPDATE SKIP LOCKED`) → incrementa `processing_generation` → `pgmq.send('q_inbound', {conversation_id, generation, target_seq})` → limpa `pending_response_at`. Ou tudo comita (job existe), ou nada (prazo continua vencido e o próximo tick tenta de novo).
- **Dedup é validação no worker, não a chave:** pgmq não interpreta `conversation_id:generation` como restrição de unicidade — é uma fila com visibility timeout, e redelivery é comportamento normal quando um job não é arquivado. Antes de chamar o LLM, o worker verifica e arquiva sem processar quando: `job.generation != conversation.processing_generation` (job obsoleto); `job.target_seq <= conversation.last_processed_seq` (job já concluído — regra dura: `if job.target_seq <= last_processed_seq: archive_without_processing()`); `job.target_seq < conversation.next_inbound_seq` (mensagem nova chegou; o debounce reagendado gera job novo). Opcional se surgir necessidade: tabela `conversation_jobs (conversation_id, generation, target_seq, status, UNIQUE(conversation_id, generation))` para formalizar a dedup — os checks acima bastam no MVP. Rajada de 5 mensagens = 1 processamento; nenhum worker dorme.

### ADR-8 — Outbox de envio: evitar perda e minimizar duplicidade
- **Meta honesta:** a outbox garante atomicidade **dentro do nosso banco**; ela não fornece exactly-once contra uma API externa sem idempotência comprovada. A garantia da plataforma é: **nenhum envio perdido; duplicidade minimizada por deduplicação interna, reconciliação e tratamento conservador de `unknown`; risco residual de duplicata documentado e aceito.**
- **Fato verificado nesta revisão:** o endpoint padrão de envio da Cloud API **não tem parâmetro documentado de idempotência**. O que existe é `biz_opaque_callback_data` — carregamos nele nossa `idempotency_key`, o que permite correlacionar os **webhooks de status** da Meta com o item da outbox. Teste de integração para confirmar comportamento real fica nas pendências (§10).
- **Fluxo do sender (estado `sending` com lease própria):**

  ```
  pending → sending  claim: locked_by, locked_until, request_started_at,
                      next_attempt_at, payload_hash (FOR UPDATE SKIP LOCKED)
       → API ok      → sent (provider_message_id)
       → API erro    → failed → retry com backoff ou descarte com alerta
       → sem resposta/timeout, ou locked_until expirou → unknown
  ```

- **Política de `unknown` — nunca reenviar cego:**
  1. há `provider_message_id` (wamid)? → **aguardar e correlacionar o status webhook** (o mecanismo documentado pela Meta); consulta direta por mensagem só se um endpoint oficialmente suportado for validado no teste de integração — até lá é hipótese, não capacidade;
  2. sem id → aguardar webhook de status correlacionado por `biz_opaque_callback_data` (janela de espera);
  3. sem evidência após a janela → fila de **revisão manual** no admin;
  4. risco residual de duplicata: aceito e documentado (uma mensagem duplicada rara é melhor que uma cobrança de PIX enviada duas vezes por retry cego — por isso o conservadorismo).
- `agent_core` e `dispatch` **nunca chamam a API do canal** — só gravam na outbox (na transação da FASE 3).

### ADR-9 — Protocolos
- **Externo (in):** webhooks REST (plataformas, Meta, Evolution). **Externo (out):** REST (Meta, Evolution, APIs das lojas, rastreio, LLM) — mensagens sempre via sender/outbox.
- **Hub ↔ banco:** Supabase client com RLS + Realtime para o inbox. **Hub ↔ runtime:** HTTP interno com service token, só para interações síncronas (chat de teste, rodar cenário); mudanças de estado vão pelo banco.
- **Rejeitados:** GraphQL e gRPC.

### ADR-10 — Config e prompt versionados append-only
- `agent_versions` imutável; cada mudança (Bruno, lojista, flywheel) cria versão nova com `parent_version_id`, autor e origem. Agente roda `tenants.active_version_id`; rollback = mover o ponteiro. Trilha de auditoria de graça.

### ADR-11 — Multi-tenancy e segurança de acesso ao banco
- **Shared schema + tenant_id + RLS** em todas as tabelas de negócio; hub com JWT do usuário.
- **Roles do runtime — o Postgres precisa distinguir módulos, não confiar neles:**
  1. **`worker_role`** — tabelas de negócio (RLS via `SET LOCAL app.tenant_id`) + `get_connector_secret(account_id)`;
  2. **`sender_role`** — `message_outbox` e tabelas de envio + `get_channel_secret(account_id)`;
  3. **connection pools separados** por role; nenhum role da aplicação tem `BYPASSRLS` nem é dono das tabelas protegidas; service role não circula na aplicação.
- **Segredos:** **nenhum** GRANT geral em `vault.decrypted_secrets` (quem lê a view lê tudo decifrado). Acesso só por funções `SECURITY DEFINER` escopadas (`get_channel_secret`, `get_connector_secret`) que: fixam `SET search_path = trusted_schema, pg_temp`; usam nomes totalmente qualificados; validam tenant e finalidade internamente; têm `EXECUTE` revogado de `PUBLIC` e concedido só ao role certo.
- **Princípio do claim para acesso cross-tenant:** com RLS + `SET LOCAL app.tenant_id` e sem `BYPASSRLS`, nenhum role da aplicação consegue (nem deve) fazer `SELECT` global. Todo trabalho que precisa varrer tenants — polling da outbox, leitura de filas, purgas, reconciliação, operações administrativas — acontece **exclusivamente por funções `SECURITY DEFINER` de claim**, sem `SELECT` direto nas tabelas. Exemplo canônico: `claim_outbox_batch(p_worker_id, p_limit)` seleciona itens disponíveis com `FOR UPDATE SKIP LOCKED`, grava `locked_by`/`locked_until`/status e retorna **apenas** as linhas atribuídas — sem filtros arbitrários, sem expor dados de outros tenants. Após o claim, cada item é trabalhado no contexto do seu tenant. Mesma higiene das demais funções: `search_path` fixo, nomes qualificados, EXECUTE revogado de PUBLIC.
- **Tabelas internas** (outbox, filas, evals) fora do schema exposto pela Data API; lint no CI proíbe SQL fora da camada de repositório; views do hub com `security_invoker`; suíte de vazamento cross-tenant roda com JWT, `worker_role` e `sender_role`.
- **PII sensível (CPF, nascimento) — decisão em duas camadas:** (1) **default da vertical e-commerce: NÃO coletar.** O melhor controle é não ter o dado; o fluxo de escalonamento repassa a conversa sem persistir documento. (2) Se algum caso exigir coleta, a decisão de produção é **envelope encryption**: chave de dados por tenant, cifrada por KMS, com rotação e auditoria de cada decifração, decifrável só pelo fluxo de escalonamento. Chave estática em variável de ambiente é aceitável **apenas em desenvolvimento** — nunca é a decisão de produção. Demais dados: cifra em repouso padrão.

### ADR-12 — Uso secundário de conversas (treino/benchmark): SUSPENSO até decisão própria
- **Contexto:** "anonimizar e copiar para treino" é juridicamente frágil — remover nome/telefone/CPF não anonimiza: pedidos, produtos, cidade, datas e rastreios permitem reidentificação. Pela LGPD, dado reversível com esforço razoável continua pessoal; a ANPD recomenda avaliação de risco.
- **Decisão interina (vigente até o ADR definitivo):** purga de lojista cancelado = **hard delete sem cópia integral**. Para avaliação e melhoria usam-se apenas: cenários sintéticos; conversas **selecionadas manualmente e desidentificadas caso a caso**; métricas agregadas; avaliações que não retêm o texto integral.
- **O ADR definitivo deverá definir:** finalidade; base legal e autorização (contrato com lojista + aviso ao titular); campos removidos; teste de risco de reidentificação; separação dataset de avaliação × treinamento; retenção; controle de acesso; propagação do direito de exclusão; se dados vão a provedores de LLM. Até lá, nada de cópia permanente.

---

## 2. Diagrama de componentes (texto)

```
[Shopify/Nuvemshop/Yampi]──webhooks──┐
[Meta Cloud API]──webhooks───────────┤
[Evolution API]──webhooks────────────┼──▶ [Edge Functions: /wh/{fonte}]
                                     │      valida HMAC → ingest_webhook() ── 1 transação:
                                     │        ON CONFLICT → evento de plataforma: q_domain_events
                                     │                    → inbound: messages(seq) +
                                     │                      pending_response_at (SEM enfileirar)
                                     ▼
                  ┌──────────────────────────────────────────┐
                  │            SUPABASE POSTGRES             │
                  │  dados · agent_versions · pgvector       │◀── RLS/JWT ── [Hub Next.js/Vercel]
                  │  q_inbound·q_domain·q_sched·q_evals+DLQs │──Realtime──▶ (form · hub · admin)
                  │  message_outbox · Vault (via funções)    │
                  └───────────────┬──────────────────────────┘
                                  │ worker_role / sender_role · SET LOCAL app.tenant_id
                                  ▼
                  ┌──────────────────────────────────────────┐
                  │   RUNTIME PYTHON (VPS) — 1 processo      │
                  │   asyncio: workers (weighted polling     │
                  │   8:4:2:1 + aging) · coalescer (2s,      │
                  │   transação única c/ generation) ·       │
                  │   scheduler · senders (lease + unknown)  │──REST──▶ Meta / Evolution
                  │   judges/evals · API interna (testes)    │──REST──▶ lojas / rastreio / LLM
                  └──────────────────────────────────────────┘
```

---

## 3. Módulos e fronteiras (dentro do runtime Python)

Regra de ouro: módulos se falam por interfaces; `channels` e `connectors` são **portas com adaptadores** — onde o roadmap cresce (e-mail, Instagram DM, novas plataformas).

| Módulo | Responsabilidade | Interface (porta) |
|---|---|---|
| `channels` | senders da outbox, typing, botões, delay humanizado | `enqueue_send(msg)`; adaptadores: `whatsapp_cloud`, `whatsapp_evolution`; futuros: `email`, `instagram_dm` |
| `connectors` | dados da loja | `get_order`, `get_customer`, `get_products`, `list_abandoned`; adaptadores: `shopify`, `nuvemshop`, `yampi` |
| `agent_core` | prompt em camadas, slots, think-gate, LLM; fases claim/trabalho/CAS | `respond(conversation, pending_msgs) -> draft` |
| `tools` | leque por tenant (pedido, produto, rastreio, salvar contato, chamar humano, agendar) | registry: tenant escolhe subset |
| `judges` | Judge 1 pré-envio + judges assíncronos + flywheel | `pre_send(draft)`, `post_hoc(conversation)` |
| `dispatch` | funis, cadência, follow-up, supressão, anti-ban | consome `q_domain_events`/`q_scheduled` |
| `queueing` | pgmq (read/set_vt/archive/DLQ), backoff, heartbeat, weighted polling + aging, semáforos por tenant | usado por todos os consumidores |
| `inbox` | takeover humano, modo observador, devolver-para-IA | flags na conversa |
| `onboarding` | agente gerador de prompt, gate duplo | cria `agent_versions` draft |
| `quota` | enforcement points por tenant — default ilimitado | `check(tenant, ação)` |
| `obs` | logs: tool calls, latência, custo, scores, filas, DLQ, outbox | tabelas de observabilidade |

**Cross-channel (futuro):** `dispatch` opera sobre `contact_id`, não sobre número.

---

## 4. Modelo de dados (tabelas principais)

Todas com `tenant_id` + RLS (JWT, `worker_role` e `sender_role`), salvo indicação.

- `tenants` — status, `active_version_id`, retenção (12–24 meses), "nunca dizer que é IA", follow-up on/off.
- `memberships` — usuário × tenant × role.
- `agent_versions` — append-only: prompts, tools, identidade, autor, origem, `parent_version_id`, status.
- `channels_accounts` — número, tipo (cloud|evolution), ref. Vault, tier Meta, warm-up, teto diário.
- `connector_accounts` — plataforma, ref. OAuth no Vault, estado do sync.
- `contacts` — telefone normalizado, nome, idioma, opt-in/out.
- `suppression_list` — contato × tenant × motivo × timestamp.
- `conversations` — estado (`ia|humano|encerrada`), ocasião, slots, **`pending_response_at`**, **`last_processed_seq`**, **`next_inbound_seq`**, **`next_outbound_seq`**, **`processing_generation`**, **`processing_token`**, **`processing_until`**, **`version`** (CAS).
- `messages` — direção, conteúdo, canal, **`seq`** com **`UNIQUE (conversation_id, direction, seq)`**, `expires_at` (TTL rolante).
- `message_outbox` — payload, `idempotency_key`, status (`pending|sending|sent|failed|unknown`), attempt_count, `provider_message_id`, **`locked_by`**, **`locked_until`**, **`next_attempt_at`**, **`last_error`**, **`request_started_at`**, **`payload_hash`**, created_at, sent_at. **Fora da Data API.**
- `webhook_events` — payload bruto, fonte, `source_account_id`, `external_event_id` — **UNIQUE (source, source_account_id, external_event_id)**, status.
- pgmq: `q_inbound`, `q_domain_events`, `q_scheduled`, `q_evals` + `_dlq`. **Fora da Data API.**
- `scheduled_touches` — toques com `due_at`.
- `orders`, `customers`, `products` — sync das plataformas.
- `knowledge_base` — chunks + embeddings (pgvector).
- `scenarios`, `eval_runs`, `judge_scores`, `tool_calls`, `llm_calls` — evals e observabilidade.
- `audit_log` — ações do hub (inclui aceite do risco Evolution).
- *(gatilho de escala)* `tenant_slots` — lease distribuída do semáforo quando houver multi-processo.

---

## 5. Fluxos críticos

### 5.1 Mensagem inbound (suporte/conversa)
1. Webhook do canal → Edge Function → `ingest_webhook()`: grava `messages` (seq atômico) + `pending_response_at = now()+10s`. **Não enfileira.** Mensagem nova só empurra o prazo.
2. **Coalescer** (tick 2s, transação única): conversa vencida → `processing_generation++` → job `{conversation_id, generation, target_seq}` em `q_inbound` → limpa `pending_response_at`.
3. Worker: valida generation/target_seq (obsoleto → archive) → **FASE 1** claim da lease (transação curta) → **FASE 2** carrega tudo (config ativa, mensagens > `last_processed_seq`, slots, contexto, idioma) e chama LLM/tools **fora de transação**, com heartbeat de lease e de VT.
4. Takeover humano: modo observador — registra, atualiza slots, não responde.
5. Judge 1 pré-envio (reprova → regenera; violação crítica pós-envio → auto-correção + alerta).
6. **FASE 3** transação curta com CAS estendido (`token` + `version` + `generation` + `next_inbound_seq = target_seq`): avança `last_processed_seq` + INSERT mensagem outbound (seq atômico) + INSERT `message_outbox` + slots. CAS falhou (inclusive por mensagem nova chegada durante o LLM) → draft descartado, lease liberada se o token bater, e o novo job do debounce responde ao conjunto completo.
7. Sender entrega com typing + delay humanizado. Loga tudo.

### 5.2 Evento de abandono (carrinho/checkout/PIX)
1. Webhook → `ingest_webhook()` → `q_domain_events`.
2. Worker (`internal.apply_domain_event`): reentrega já aplicada? tipo suportado? telefone E.164? canal ativo? A ocasião tem **funil habilitado** para o tenant? Não → desfecho `no_funnel`, sem nada gravado. Desfechos são dado, não exceção; só um job apontando para evento inexistente levanta.
3. `internal.start_funnel_run` materializa a **cadência inteira** do funil em `scheduled_touches`, numa transação única — contato, conversa (reusada se aberta) e uma linha por toque de `funnels.touches`, com `due_at = event_at + delay` e `event_at` = o instante do **evento**, nunca o do agendamento.
4. **Nenhum toque vai direto para a outbox** (E3 · D11). O toque nº 1 nasce vencido (`delay = PT0S` ⇒ `due_at` no passado) e o dispatcher o pega no tique seguinte; um evento atrasado materializa uma cadência já vencida, e é a escada que decide se ela ainda sai. O primeiro toque atravessa exatamente as mesmas proteções que o quarto: **existe uma porta de saída só**. A versão anterior deste fluxo mandava o primeiro toque via outbox e só os seguintes para `scheduled_touches` — o que fazia o primeiro toque de todo funil pular a escada, contra o RF-033 (supressão checada antes de TODO envio proativo) e o RF-034 (a janela de 24h soma TODAS as origens).
5. Dispatcher (§5.4): supressão → quota → staleness (§5.3) → limites, com os guards revalidados no `WHERE` da gravação. Pago nesse meio-tempo? nada sai, toque cancelado com motivo (evento de pagamento também cancela funil ativo — por isso `q_domain_events` tem promoção por idade).
6. Cloud API → template aprovado + botões Autorizar/Bloquear em contato novo. Evolution → anti-ban (§5.5).
7. Resposta do contato mata toques futuros e vira conversa normal (5.1).

### 5.3 Drenagem pós-queda (staleness check)
Evento com idade > 5 min: mensagem mais recente depois do evento? Pedido pago? Contato em supressão? → archive com log.

### 5.4 Rate limits e proteção de contato (default 1/24h · teto da plataforma 4/24h)
- **Default:** máx. 1 toque proativo/contato/24h (todas as origens). **Teto absoluto da plataforma: 4 toques proativos/contato/24h** — afrouxar o default até o teto é ação exclusiva do admin, por tenant; o lojista no hub só pode apertar, nunca afrouxar (trava de segurança não enfraquece por config de cliente — mesmo princípio do Judge 1).
- **Onde o teto vive (E3 · S2):** no banco, não na aplicação. `tenants.proactive_max_per_contact_24h smallint NOT NULL DEFAULT 1 CHECK (BETWEEN 1 AND 4)` — nenhum caminho de escrita, em nenhuma camada, consegue passar de 4. A coluna tem **um único caminho de escrita**: `internal.set_proactive_cap(tenant, valor, ator)` (SECURITY DEFINER, `search_path` fixo, EXECUTE revogado de PUBLIC e concedido a ninguém — o plano de admin conecta com credencial própria), que grava `audit_log`. Nenhum role da aplicação tem `UPDATE` em `tenants`, e um **trigger** recusa qualquer alteração da coluna que não venha da função: o privilégio sozinho não é durável, porque um `GRANT UPDATE ON tenants` para a tela de configurações do E5 devolveria a coluna ao lojista sem que nenhum teste percebesse.
- **Onde cada janela é medida:** a de **24h por contato** soma todas as origens e por isso é contada em `internal.message_outbox` (`kind in ('funnel_touch','followup')`, por `created_at` — o compromisso de envio, não a entrega); a de **72h entre funis distintos** é contada em `scheduled_touches.sent_at`, porque a identidade do funil não existe em nenhuma outra tabela. `scheduled_touches.outbox_id` liga as duas, para que nunca discordem sobre se um toque aconteceu.
- **Não há limite de total de toques por funil:** o total vem da cadência configurada do funil. As proteções por contato valem sempre: janela de 24h, cooldown 72h entre funis, supressão automática após 3 disparos sem resposta em funis distintos.
- Reativas nunca bloqueadas por rate limit; anti-flood só via debounce.
- Cloud API: token bucket por número no tier da Meta; a 80%, pausa proativos + alerta.

### 5.5 Anti-ban (Evolution)
Variação de copy por LLM a cada disparo; jitter 30–120s por número; warm-up crescente (20→50→100...); teto duro diário (default 300); aceite do risco em `audit_log`.

### 5.6 Onboarding e gate duplo
Admin cadastra → link do formulário (define quem conecta o número) → cliente conecta loja (OAuth) e WhatsApp no próprio formulário, antes de existir conta → agente gerador cria `agent_versions` draft, conectado porém pausado → Bruno testa e aprova → cliente testa, ajusta e aprova → ativa → e-mail → senha → hub → shadow de 7 dias (100% avaliado, sem reter envio).

### 5.7 Flywheel de melhoria
Judges assíncronos (`q_evals`) → `judge_scores` → patches viram `agent_versions` (origem: flywheel) → gate do Bruno → ativação. Respeitando o ADR-12: sem cópia permanente de conversas até o ADR de uso secundário.

---

## 6. Jobs do runtime (tasks do processo único)

1. **Coalescer** (2s): transação única claim+generation+send+clear (ADR-7).
2. **Senders** (loop por canal): drenam outbox com lease; `unknown` → reconciliador.
3. **Reconciliador de outbox** (5 min): com ou sem `provider_message_id`, o caminho primário é aguardar/correlacionar o **status webhook** (por wamid ou `biz_opaque_callback_data`); consulta direta só se validada em integração; sem evidência após a janela → fila de revisão manual. Nunca reenvia cego. Acesso via `claim_outbox_batch`.
4. **Reconciliação de plataformas** (15 min): poll → `ingest_webhook()`.
5. **Dispatcher de toques** (1 min): `scheduled_touches` vencidos → `q_scheduled`.
6. **Sync** de catálogo/pedidos/clientes + Google Sheets + upload CSV/XLSX.
7. **Purga TTL** (diário): `messages.expires_at` vencido → delete.
8. **Purga de lojista** (diário): cancelados ≥10 dias → **hard delete sem cópia integral** (ADR-12 interino).
9. **Health/alertas:** filas (profundidade/idade), DLQs, outbox `unknown|failed` e fila de revisão, tier Meta, conectores, leases expiradas.
10. **Purga LGPD manual** por telefone via função SQL restrita; opt-out ≠ apagamento.

---

## 7. NFRs — como cada um é atendido

| Requisito | Solução |
|---|---|
| Não perder eventos após confirmação da ingestão; reconciliar ausentes quando a API de origem permitir | Ingestão HA fora da VPS + `ingest_webhook()` atômica e idempotente por conta de origem + reconciliação por poll (abandonos/pedidos); limitação documentada: inbound WhatsApp sem webhook entregue não tem replay |
| **Envio: evitar perda, minimizar duplicidade** | Outbox transacional + dedup interna + lease em `sending` + reconciliação de `unknown` (id → consulta; sem id → status webhook via `biz_opaque_callback_data`; sem evidência → revisão manual) + **risco residual de duplicata documentado** (idempotência da Cloud API não comprovada) |
| Burst 20–50x intra-hora | Filas absorvem; weighted polling 8:4:2:1 + promoção por idade; concorrência por tenant; degrada em latência, nunca em perda — e sem starvation de pagamentos/cancelamentos |
| Ordem correta nas conversas | Lease + CAS + `seq` por contadores atômicos + `UNIQUE(conversation_id, direction, seq)` + coalescência com generation |
| Transações curtas sempre | LLM/tools/APIs externas nunca dentro de transação; claim e conclusão são UPDATEs de milissegundos |
| RPO próximo de zero | PITR + pg_dump externo + migrations reversíveis + restore para projeto novo + runbook trimestral com **dois caminhos** (§7b) |
| VPS pode cair (só processamento) | Tasks stateless; leases expiram sozinhas; drenagem com staleness check; alerta de fila crescendo |
| Qualidade > custo de infra | Judge 1 em 100% das respostas; shadow de estreia; logs completos |
| Limites por plano futuros | Módulo `quota` pronto, default ilimitado |
| Multi-atendente com permissões | `memberships` + roles + RLS + `audit_log` |
| Cliente edita prompt e vale de fato | `agent_versions` nova → ponteiro; rollback em 1 clique |
| Segurança de acesso ao banco | `worker_role`/`sender_role` + pools separados + RLS também no runtime + segredos só por funções escopadas + views `security_invoker` + sem BYPASSRLS |

**7b. Runbook de restore — dois caminhos obrigatórios:**
1. **Físico / PITR / restore para projeto novo (gerenciado):** preserva a chave-raiz do Vault → segredos legíveis após restauração.
2. **Lógico / pg_dump:** a chave-raiz é por projeto — dump restaurado manualmente carrega segredos **indecifráveis** sem tratamento específico da chave. O runbook documenta o procedimento e assume re-provisionamento de segredos como fallback. Backups baixáveis podem não conservar senhas de roles customizados (`worker_role`, `sender_role`) → passo de redefinição incluído.

---

## 8. Fitness functions (CI / periódicos)

1. **Fronteiras de módulo:** falha se `channels` importar `connectors` (e pares proibidos).
2. **RLS triplo:** vazamento cross-tenant testado com JWT, `worker_role` e `sender_role`.
3. **SQL ad hoc:** lint proíbe query fora da camada de repositório.
4. **Ingestão:** p99 < 500ms sob 50 ev/s; replay do mesmo webhook 3x → 1 processamento; **inbound não gera job direto** (teste de contrato do ADR-3).
5. **Coalescer:** matar o processo entre claim e send em staging → nenhum job perdido nem duplicado (transação única); rajada de 5 mensagens → exatamente 1 chamada de LLM.
6. **Lease/CAS:** duas tasks disputando a mesma conversa → 1 resposta; lease expirada no meio do trabalho → draft descartado, nada enviado; **mensagem nova injetada durante a FASE 2 → CAS falha, draft descartado, resposta seguinte cobre o conjunto completo** (teste do invariante central); redelivery do mesmo job pelo pgmq → archive sem segunda chamada de LLM.
7. **Fila:** rajada 50x drena com pesos respeitados; mensagem envenenada → DLQ + alerta; domain_event envelhecido fura a fila de inbound (teste da promoção por idade).
8. **Outbox:** matar o sender no meio do envio → item vai a `unknown` e **não** é reenviado sem evidência; duplicata só com aceite manual.
9. **Restore:** drill trimestral dos **dois caminhos** (PITR e pg_dump), cronometrado, incluindo leitura de segredo do Vault pós-restore.

---

## 9. Gatilhos de evolução

| Sinal | Ação |
|---|---|
| Fila sustentada > ~50 msg/s ou lock contention | Fila → Redis/SQS (interface do `queueing` não muda) |
| Necessidade de 2ª VPS ou multi-processo | **Pré-requisito:** semáforo por tenant migra de memória para lease no Postgres (`tenant_slots`) ou Redis — não escalar antes disso |
| Starvation real apesar de pesos+aging | Claim SQL balanceado ou dispatcher central |
| Analytics pesando no primário | Read replica |
| Canal novo com escala divergente comprovada | Extrair só aquele adaptador (strangler fig) |
| >10 tenants enterprise exigindo isolamento | Avaliar projeto Supabase dedicado por tier |

---

## 10. Pendências (por ordem de urgência)

1. **ADR de uso secundário de conversas** (treino/benchmark) — finalidade, base legal, teste de reidentificação, retenção, acesso, exclusão, envio a provedores de LLM. Até lá vale o interino do ADR-12. **Bloqueia** qualquer pipeline de retenção anonimizada.
2. **Teste de integração de duplicidade na Cloud API** — confirmar comportamento real de reenvio e de `biz_opaque_callback_data` (a idempotência não é documentada; a garantia atual assume que não existe).
3. **API de rastreio** — recomendação: 17TRACK default; Correios direto onde couber. Validar custo/tracking.
4. **Valores finais dos rate limits** — defaults em §5.4; ajustar com dados do shadow.
5. **LLM do agente** — decisão exclusiva do Bruno (arquitetura agnóstica; `llm_calls` registra provider/modelo/custo).
6. **Retenção default** — range 12–24 meses; sugestão: 12 default, configurável até 24.
7. **Coleta de CPF/nascimento** — decisão de produto pendente do Bruno: a vertical e-commerce realmente precisa armazenar? Default arquitetural: não coletar (escalonamento repassa sem persistir). Se precisar: implementar o envelope encryption do ADR-11 + modelo de ameaça (quem decifra, onde, com qual auditoria).

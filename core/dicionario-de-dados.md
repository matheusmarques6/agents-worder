# Dicionário de Dados — Atributos por Entidade

**Versão:** 1.0 · **Data:** 2026-08-01 · **Base:** Arquitetura v1.3 + Requisitos v1.2
Este documento é o passo imediatamente anterior ao schema SQL: cada atributo abaixo vira coluna.

**Convenções globais:**
- PK: `id uuid DEFAULT gen_random_uuid()`, salvo indicação (logs de alto volume usam `bigint IDENTITY`).
- Todo timestamp é `timestamptz`; toda tabela tem `created_at timestamptz DEFAULT now()`.
- Enums implementados como `text` + `CHECK`, para evoluir sem `ALTER TYPE`.
- Toda tabela de negócio tem `tenant_id uuid NOT NULL REFERENCES tenants(id)` + política RLS (JWT, `worker_role`, `sender_role`); exceções indicadas.
- Tabelas marcadas **[interna]** ficam fora do schema exposto pela Data API; acesso só por funções de claim.
- Telefones sempre normalizados E.164; valores monetários `numeric(12,2)`; moeda ISO-4217.

---

## 1. Identidade e acesso

### 1.1 `tenants`
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| name | text NOT NULL | nome da loja/lojista |
| status | text CHECK | `onboarding \| shadow \| active \| paused \| cancelled` |
| active_version_id | uuid FK → agent_versions | nullable até a 1ª aprovação; ponteiro da versão que roda |
| retention_months | smallint DEFAULT 12 | CHECK entre 12 e 24 — TTL rolante das mensagens |
| never_say_ai | boolean DEFAULT true | config por tenant |
| followup_enabled | boolean DEFAULT false | follow-up proativo opcional |
| primary_language | text DEFAULT 'pt-BR' | idioma principal do agente |
| shadow_until | timestamptz | fim dos 7 dias de estreia (100% avaliado) |
| cancelled_at | timestamptz | inicia a contagem dos 10 dias para hard delete |
| created_at / updated_at | timestamptz | |

### 1.2 `profiles` (complementa `auth.users` do Supabase Auth)
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| user_id | uuid PK FK → auth.users | Supabase Auth gerencia e-mail/senha/sessão |
| full_name | text | |
| is_platform_admin | boolean DEFAULT false | Bruno/equipe da plataforma |
| created_at | timestamptz | |

### 1.3 `memberships`
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| tenant_id | uuid FK | |
| user_id | uuid FK → auth.users | |
| role | text CHECK | `owner \| manager \| attendant` |
| permissions | jsonb DEFAULT '{}' | permissões finas configuráveis (RF-047) |
| created_at | timestamptz | UNIQUE (tenant_id, user_id) |

### 1.4 `audit_log` — append-only
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | bigint IDENTITY PK | |
| tenant_id | uuid FK nullable | null = ação de plataforma |
| actor_type | text CHECK | `user \| system \| flywheel` |
| actor_user_id | uuid FK nullable | |
| action | text NOT NULL | ex.: `agent.approve`, `agent.pause`, `prompt.edit`, `evolution.risk_accepted`, `contact.purge` |
| target_type / target_id | text / uuid | entidade afetada |
| payload | jsonb | detalhes (diff, aceite de risco, motivo) |
| created_at | timestamptz | index (tenant_id, created_at) |

---

## 2. Agente e configuração

### 2.1 `agent_versions` — append-only (só `status`/`activated_at` mutam)
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| tenant_id | uuid FK | |
| parent_version_id | uuid FK → agent_versions | nullable (1ª versão); forma a árvore de navegação |
| status | text CHECK | `draft \| active \| archived` — só um `active` por tenant (índice parcial UNIQUE) |
| origin | text CHECK | `onboarding \| bruno \| lojista \| flywheel` |
| author_user_id | uuid FK nullable | quem criou (trilha de auditoria) |
| model | text NOT NULL default `claude-sonnet-5` | modelo de chat DESTE tenant, roteado por OpenRouter (D1 emendado, decisão 79). CHECK de não-vazio; não é enum porque o catálogo do provedor muda mais rápido que o schema. **Judge 1 não está aqui**: é fixo da plataforma (`claude-haiku-4-5`) — portão de segurança que o cliente reconfigura não é portão |
| base_prompt | text NOT NULL | prompt-base da arquitetura em camadas |
| scenario_prompts | jsonb | por ocasião: `{pix_pending, checkout_abandoned, cart_abandoned, direct}` |
| identity | jsonb | nome do agente, preset de tom (3), nível de emoji (3), blacklist de palavras, aberturas |
| enabled_tools | text[] | subset do leque: `get_order, get_products, tracking, save_contact, call_human, schedule` |
| price_policy | text CHECK | `free \| total_only \| never \| if_asked` |
| product_presentation | jsonb | link/foto/texto, máx. links, fallback |
| escalation_config | jsonb | canal, gatilhos, formato do payload |
| scheduling_config | jsonb | horários, validações, regras especiais |
| change_summary | text | resumo humano do diff em relação ao parent |
| created_at / activated_at | timestamptz | |

### 2.2 `onboarding_invites`
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| tenant_id | uuid FK | |
| token | text UNIQUE NOT NULL | slug do link do formulário |
| official_number_connected_by | text CHECK | `admin \| client` — definido pelo Bruno ao gerar o link |
| status | text CHECK | `sent \| in_progress \| completed \| expired` |
| expires_at | timestamptz | |
| created_at / completed_at | timestamptz | |

### 2.3 `form_responses`
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| invite_id | uuid FK → onboarding_invites | |
| tenant_id | uuid FK | |
| answers | jsonb NOT NULL | respostas da taxonomia (identidade, objetivo, intents, preço, catálogo, apresentação, escalonamento, agendamento, KB) |
| attachments | jsonb | refs no Storage (estoque anexado) |
| client_email | text | informado ao final para criar a conta |
| submitted_at | timestamptz | |

### 2.4 `scenarios` **[interna]** — nasce em `internal`, com a trilha de avaliação (decisão 80)
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| tenant_id | uuid FK nullable | null = pack base global mantido pelo admin |
| origin | text CHECK | `base_pack \| ai_variation \| manual` |
| occasion | text | ocasião que o cenário simula |
| title | text | |
| script | jsonb NOT NULL | turnos simulados do contato |
| expected | jsonb | critérios de aprovação usados pelo Judge |
| active | boolean DEFAULT true | |
| created_at | timestamptz | |

### 2.5 `knowledge_chunks`
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| tenant_id | uuid FK | |
| source | text CHECK | `faq \| policy \| upload` |
| title | text | |
| content | text NOT NULL | |
| embedding | vector(1536) | dimensão congelada por D2 (`text-embedding-3-small` via OpenRouter); index HNSW cosseno. O tipo carrega a dimensão de propósito: `vector` puro aceitaria qualquer coisa e só quebraria ao construir o índice |
| metadata | jsonb | |
| created_at | timestamptz | |

---

## 3. Canais e conectores

### 3.1 `channels_accounts`
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| tenant_id | uuid FK | |
| type | text CHECK | `cloud \| evolution` |
| phone_e164 | text NOT NULL | UNIQUE (type, phone_e164) |
| external_account_id | text NOT NULL | id da conta no provedor — `phone_number_id` na Cloud API, nome da instância na Evolution. **É a chave que resolve o tenant na ingestão de canal**, o equivalente do `source_account_id` do conector; UNIQUE (type, external_account_id) |
| display_name | text | |
| vault_secret_id | uuid | ref. no Vault — lida só via `get_channel_secret()` |
| status | text CHECK | `connecting \| active \| paused \| banned \| error` |
| meta_tier | integer | tier de conversas/24h (Cloud) |
| tier_usage_24h | integer DEFAULT 0 | contador corrente; a 80% pausa proativos + alerta |
| warmup_stage | integer DEFAULT 0 | estágio do warm-up (Evolution) |
| warmup_started_at | timestamptz | |
| daily_cap | integer DEFAULT 300 | teto duro diário de proativos (Evolution) |
| risk_accepted_at | timestamptz | aceite do risco de banimento (Evolution); espelhado no audit_log |
| created_at | timestamptz | |

### 3.2 `connector_accounts`
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| tenant_id | uuid FK | |
| platform | text CHECK | `shopify \| nuvemshop \| yampi` |
| source_account_id | text NOT NULL | id da loja na plataforma — **é a chave que resolve o tenant na ingestão**; UNIQUE (platform, source_account_id) |
| vault_secret_id | uuid | OAuth — lido só via `get_connector_secret()` |
| sync_status | text CHECK | `ok \| syncing \| error` |
| last_sync_at | timestamptz | |
| webhooks_registered | boolean DEFAULT false | |
| created_at | timestamptz | |

### 3.3 `orders` (espelho sincronizado)
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| tenant_id | uuid FK | |
| connector_account_id | uuid FK | UNIQUE (connector_account_id, external_id) |
| external_id | text NOT NULL | id do pedido na plataforma |
| customer_external_id | text | liga ao `customers.external_id` |
| status | text | fulfillment/status geral |
| financial_status | text | `pending \| paid \| refunded...` — pago cancela funil |
| total | numeric(12,2) / currency text | |
| items | jsonb | linhas do pedido |
| tracking_code | text | usado pela tool de rastreio |
| tracking_status | text | último status conhecido |
| platform_created_at / platform_updated_at | timestamptz | |
| synced_at | timestamptz | |

### 3.4 `customers` (espelho sincronizado — contexto injetado no prompt)
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| tenant_id | uuid FK | |
| connector_account_id | uuid FK | UNIQUE (connector_account_id, external_id) |
| external_id | text NOT NULL | |
| name / email | text | |
| phone_e164 | text | liga ao `contacts.phone_e164` |
| total_orders | integer | contexto: total de compras |
| total_spent | numeric(12,2) | |
| avg_ticket | numeric(12,2) | contexto: ticket médio |
| first_order_at / last_order_at | timestamptz | contexto: primeira compra |
| synced_at | timestamptz | |

### 3.5 `products` (espelho: plataforma, CSV/XLSX ou Google Sheets)
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| tenant_id | uuid FK | |
| connector_account_id | uuid FK nullable | null quando a fonte é csv/gsheets sem loja ligada |
| external_id | text | UNIQUE (connector_account_id, external_id) quando houver |
| source | text CHECK | `platform \| csv \| gsheets` |
| title / description | text | |
| price / compare_at_price | numeric(12,2) | |
| stock | integer | inventory hard rule do agente lê daqui |
| images | jsonb | URLs |
| url | text | link do produto para apresentação |
| category | text | |
| active | boolean DEFAULT true | |
| synced_at | timestamptz | |

---

## 4. Conversação

### 4.1 `contacts`
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| tenant_id | uuid FK | UNIQUE (tenant_id, phone_e164) |
| phone_e164 | text NOT NULL | |
| name | text | |
| language | text | idioma detectado do contato (agente se adapta) |
| opt_status | text CHECK | `pending \| authorized \| blocked` — botões Autorizar/Bloquear |
| customer_id | uuid FK → customers nullable | vínculo quando identificado |
| first_seen_at / last_message_at | timestamptz | |
| created_at | timestamptz | |

### 4.2 `conversations`
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| tenant_id | uuid FK | index (tenant_id, contact_id) |
| contact_id | uuid FK | |
| channel_account_id | uuid FK | |
| state | text CHECK | `ia \| humano \| encerrada` |
| origin_occasion | text CHECK | `pix_pending \| checkout_abandoned \| cart_abandoned \| direct \| campaign` |
| slots | jsonb DEFAULT '{}' | estado extraído da conversa (atualizado até em modo observador) |
| pending_response_at | timestamptz nullable | prazo do debounce; mensagem nova empurra |
| last_processed_seq | integer DEFAULT 0 | até onde o agente respondeu |
| next_inbound_seq | integer DEFAULT 0 | contador atômico (UPDATE...RETURNING) |
| next_outbound_seq | integer DEFAULT 0 | contador atômico |
| processing_generation | integer DEFAULT 0 | incrementado só pelo coalescer |
| processing_token | uuid nullable | dono da lease de processamento |
| processing_until | timestamptz nullable | expiração da lease (2 min, renovável) |
| version | integer DEFAULT 0 | contador do compare-and-set |
| takeover_user_id | uuid FK nullable | atendente que assumiu |
| takeover_at / closed_at | timestamptz | retorno ao agente é só manual |
| created_at / updated_at | timestamptz | |

### 4.3 `messages`
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| tenant_id | uuid FK | |
| conversation_id | uuid FK | **UNIQUE (conversation_id, direction, seq)** |
| direction | text CHECK | `inbound \| outbound` |
| seq | integer NOT NULL | atribuído pelos contadores atômicos |
| channel | text CHECK | `whatsapp_cloud \| whatsapp_evolution` (futuro: `email \| instagram_dm`) |
| author_type | text CHECK | `contact \| agent \| human` |
| author_user_id | uuid FK nullable | quando `human` (takeover) |
| content | jsonb NOT NULL | texto, mídia (refs), botões, template usado |
| provider_message_id | text nullable | wamid — correlação com status webhooks |
| outbox_id | uuid FK nullable | origem do envio (outbound) |
| expires_at | timestamptz NOT NULL | TTL rolante = created_at + retention do tenant; index para purga diária |
| created_at | timestamptz | |

### 4.4 `suppression_list`
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| tenant_id | uuid FK | UNIQUE (tenant_id, contact_id) |
| contact_id | uuid FK | |
| reason | text CHECK | `explicit_block \| no_response_after_3 \| intent_optout \| manual` |
| created_by | text CHECK | `system \| agent \| admin` |
| created_at | timestamptz | checada antes de TODO envio proativo; opt-out ≠ apagamento |

---

## 5. Ingestão, filas e envio

> **Onde as tabelas [interna] moram.** Fora do schema exposto pela Data API (ADR-11) — e `public` **é** exposto, está na lista de `supabase/config.toml`. Por isso `webhook_events` e `message_outbox` vivem no schema **`internal`**, e as filas no schema `pgmq`. Assim a exposição fica impossível por construção em vez de depender de ninguém escrever um GRANT errado. Materializado na migration `20260802000003_steel_thread.sql`.

### 5.1 `webhook_events` **[interna]** — schema `internal`
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | bigint IDENTITY PK | |
| source | text CHECK | `shopify \| nuvemshop \| yampi \| meta \| evolution` |
| source_account_id | text NOT NULL | **UNIQUE (source, source_account_id, external_event_id)** — IDs sequenciais por loja nunca colidem entre tenants |
| external_event_id | text NOT NULL | chave natural do evento na origem |
| tenant_id | uuid FK | resolvido pela conta de origem dentro de `ingest_webhook()` |
| event_type | text | `checkout_abandoned \| pix_pending \| order_paid \| message_inbound \| status_update ...` |
| payload | jsonb NOT NULL | bruto, como chegou |
| status | text CHECK | `received \| enqueued \| processed \| discarded \| failed` |
| received_at / processed_at | timestamptz | |

### 5.2 Filas pgmq **[interna]** — payloads canônicos
| Fila | Payload | Observação |
|---|---|---|
| q_inbound | `{conversation_id, generation, target_seq}` | criado SÓ pelo coalescer, em transação única |
| q_domain_events | `{webhook_event_id}` | abandono, pagamento, status |
| q_scheduled | `{scheduled_touch_id}` | toques vencidos |
| q_evals | `{kind, conversation_id? , eval_run_id?}` | melhor esforço |
| *_dlq (4) | mensagem original + `{error_class, last_error, failed_at}` | alerta + reprocesso manual |

### 5.3 `message_outbox` **[interna]** — schema `internal`, claim só via `claim_outbox_batch()`
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| tenant_id | uuid FK | |
| conversation_id | uuid FK nullable | toque de funil pode preceder conversa |
| contact_id | uuid FK | |
| channel_account_id | uuid FK | |
| kind | text CHECK | `reply \| funnel_touch \| followup \| correction` |
| payload | jsonb NOT NULL | conteúdo final a enviar (com variação anti-ban já aplicada) |
| idempotency_key | text UNIQUE NOT NULL | determinística; vai em `biz_opaque_callback_data` |
| payload_hash | text | detecção de mutação entre tentativas |
| status | text CHECK | `pending \| sending \| sent \| failed \| unknown \| manual_review` |
| attempt_count | integer DEFAULT 0 | |
| next_attempt_at | timestamptz | backoff |
| locked_by | text nullable | lease do sender |
| locked_until | timestamptz nullable | expirou em `sending` → `unknown` |
| request_started_at | timestamptz | |
| last_error | text | |
| provider_message_id | text nullable | wamid quando a API respondeu |
| created_at / sent_at | timestamptz | index de claim: (status, next_attempt_at) |

### 5.4 `funnels`
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| tenant_id | uuid FK | |
| occasion | text CHECK | `pix_pending \| checkout_abandoned \| cart_abandoned` |
| enabled | boolean DEFAULT true | |
| channel_preference | text CHECK | `cloud \| evolution \| auto` |
| touches | jsonb NOT NULL | `[{n, delay, template_ref/copy_base, cta}]` — copy é variada por LLM no envio Evolution |
| max_touches | integer DEFAULT 4 | teto do RF-034 |
| created_at / updated_at | timestamptz | |

### 5.5 `scheduled_touches`
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| tenant_id | uuid FK | |
| funnel_id | uuid FK | |
| contact_id | uuid FK | |
| conversation_id | uuid FK nullable | |
| touch_number | integer | |
| due_at | timestamptz NOT NULL | index (status, due_at) |
| status | text CHECK | `pending \| enqueued \| sent \| cancelled` |
| cancel_reason | text nullable | `replied \| paid \| suppressed \| manual` |
| created_at | timestamptz | |

---

## 6. Avaliação e observabilidade

### 6.1 `eval_runs`
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| tenant_id | uuid FK | |
| agent_version_id | uuid FK | versão testada |
| trigger | text CHECK | `onboarding \| manual \| seasonal \| flywheel` |
| status | text CHECK | `running \| done \| failed` |
| aggregate_score | numeric(5,2) | |
| summary | jsonb | por cenário |
| started_at / finished_at | timestamptz | |

### 6.2 `judge_scores`
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | bigint IDENTITY PK | |
| tenant_id | uuid FK | visível só ao admin (RLS) |
| kind | text CHECK | `pre_send \| post_hoc` |
| conversation_id / message_id | uuid FK nullable | avaliação de produção |
| eval_run_id / scenario_id | uuid FK nullable | avaliação sintética |
| judge_model | text | |
| score | numeric(5,2) | |
| verdict | text CHECK | `pass \| fail \| critical` — critical dispara alerta + auto-correção |
| rationale | text | |
| created_at | timestamptz | |

### 6.3 `tool_calls`
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | bigint IDENTITY PK | |
| tenant_id | uuid FK | |
| conversation_id | uuid FK | |
| message_id | uuid FK nullable | resposta que a chamada alimentou |
| tool_name | text | |
| input / output | jsonb | |
| success | boolean | |
| error | text nullable | |
| latency_ms | integer | |
| created_at | timestamptz | |

### 6.4 `llm_calls`
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | bigint IDENTITY PK | |
| tenant_id | uuid FK nullable | null em chamadas de plataforma |
| purpose | text CHECK | `agent_reply \| judge_pre \| judge_async \| prompt_generator \| copy_variation \| embedding` |
| conversation_id / eval_run_id | uuid FK nullable | |
| provider / model | text | trocar/rotear LLM é config — aqui fica o rastro |
| input_tokens / output_tokens | integer | |
| cost_usd | numeric(10,6) | |
| latency_ms | integer | |
| created_at | timestamptz | |

### 6.5 `alerts`
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| tenant_id | uuid FK nullable | null = alerta de plataforma |
| type | text CHECK | `critical_violation \| queue_depth \| queue_age \| dlq \| outbox_unknown \| outbox_review \| meta_tier \| connector_error \| lease_expired` |
| severity | text CHECK | `info \| warning \| critical` |
| title | text | |
| payload | jsonb | contexto para investigar |
| status | text CHECK | `open \| acknowledged \| resolved` |
| created_at / resolved_at | timestamptz | |

---

## 7. Placeholders previstos (nascem quando o gatilho disparar)

### 7.1 `tenant_slots` **[interna]** — só com multi-processo/2ª VPS
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| tenant_id | uuid | PK composta (tenant_id, slot_number) |
| slot_number | smallint | 1..N (default 3) |
| lease_owner | text nullable | processo dono |
| lease_until | timestamptz nullable | expira sozinha |

### 7.2 `quota_rules` — quando a regra de planos existir (RF-073)
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| tenant_id | uuid FK | |
| metric | text | `proactive_sends_month \| concurrency \| feature_x` |
| limit_value | integer | |
| window | text | `month \| day \| concurrent` |
| action_on_exceed | text | `block \| alert` |

---

## 8. Notas de integridade que o schema deve materializar

1. **Índice parcial** garantindo um único `agent_versions.status='active'` por tenant.
2. `messages.expires_at` sempre preenchido por trigger/default a partir de `tenants.retention_months`.
3. `financial_status='paid'` em `orders` é o sinal que cancela `scheduled_touches` do contato (via evento em `q_domain_events`).
4. FKs de logs de alto volume (`judge_scores`, `tool_calls`, `llm_calls`) com `ON DELETE CASCADE` a partir de conversas — a purga TTL e a purga de lojista arrastam os derivados.
5. Embeddings (`knowledge_chunks.embedding`) entram na purga LGPD por contato apenas quando derivados de conversa — chunks de FAQ/política do lojista não contêm dados de titulares.
6. Nenhuma coluna de CPF/nascimento existe neste dicionário — coerente com o default "não coletar" (pendência nº 7 da arquitetura); se a decisão mudar, entra tabela própria cifrada sob envelope encryption, nunca colunas soltas.

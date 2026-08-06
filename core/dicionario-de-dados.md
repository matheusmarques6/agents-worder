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
| proactive_max_per_contact_24h | smallint NOT NULL DEFAULT 1 | **CHECK entre 1 e 4** — teto de toques proativos por contato/24h somando todas as origens (RF-034). O 4 é o teto absoluto da plataforma e é constraint, não convenção (nota 7). Escrita só por `internal.set_proactive_cap()`; nenhum role tem `UPDATE` em `tenants` e um trigger recusa alteração que não venha da função |
| attribution_window_hours | smallint NOT NULL DEFAULT 24 | janela em que um pedido pago depois de um toque conta como receita recuperada (D8 do E3) |
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

> **Append-only é privilégio, não promessa** (materializada no E3 · S2): nenhum role recebe `UPDATE` ou `DELETE` nesta tabela — uma entrada pode ser lida, nunca editada para longe. `tenant_id` nulo = ação de plataforma, invisível a todo tenant porque as políticas comparam por igualdade e `tenant_id = null` nunca é verdadeiro (mesma forma de `alerts`).

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
| financial_status | text NOT NULL DEFAULT 'pending' CHECK | `pending \| authorized \| paid \| partially_refunded \| refunded \| voided \| cancelled` — pago cancela funil (nota 3). O vocabulário é **normalizado pelo adaptador do conector**, nunca a palavra crua da plataforma: um valor não mapeado não falharia, apenas nunca seria igual a `paid`, e o lojista seguiria cobrando quem já pagou. O CHECK é o que torna o esquecimento barulhento |
| currency | text NOT NULL DEFAULT 'BRL' | ISO-4217 (CHECK `^[A-Z]{3}$`) |
| total | numeric(12,2) | |
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
| touches | jsonb NOT NULL | `[{n, delay, copy_base[, template_ref, cta]}]` — copy é variada por LLM no envio Evolution. **Sem DEFAULT e com CHECK de forma** (`public.funnel_cadence_is_valid`, E3 · S4): lista não vazia · `n` inteiro positivo e **distinto** · `delay` em ISO-8601 (`PT0S`, `PT24H`, `P1DT6H`) porque `start_funnel_run` o converte direto em `interval` · `copy_base` obrigatório, porque texto é o que todo adaptador entrega hoje e uma entrada que só nomeia template é um toque que ninguém consegue enviar. Cadência vazia deixou de ser no-op silencioso: `enabled` é o interruptor |
| max_touches | integer DEFAULT 4 | teto da cadência **deste funil**, não limite por contato — os limites por contato são da escada (RF-034) e vivem em `tenants.proactive_max_per_contact_24h` |
| created_at / updated_at | timestamptz | **índice único parcial (tenant_id, occasion) WHERE enabled** (nota 8): `start_funnel_run` pergunta "esta ocasião tem funil habilitado?" e duas respostas não são configuração, são sorteio |

### 5.5 `scheduled_touches`
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| tenant_id | uuid FK | |
| funnel_id | uuid FK | |
| contact_id | uuid FK | |
| conversation_id | uuid FK nullable | ON DELETE SET NULL — o toque pode preceder a conversa que vai iniciar, e precisa sobreviver a uma conversa purgada, senão o cooldown de 72h esquece toques que saíram |
| order_id | uuid FK → orders nullable | o pedido que este funil persegue; sem ele o guard `order_unpaid` da escada não tem o que revalidar (E3 · D2) |
| touch_number | integer | |
| event_at | timestamptz NOT NULL | quando aconteceu o fato que justifica o toque (o abandono, o PIX não pago) — **não** quando o toque foi agendado, e sem DEFAULT de propósito: `now()` trocaria silenciosamente um pelo outro e quebraria exatamente o caso de evento atrasado que o staleness existe para pegar (RF-032) |
| due_at | timestamptz NOT NULL | index (status, due_at) |
| status | text CHECK | `pending \| enqueued \| sent \| cancelled` |
| cancel_reason | text nullable | **o vocabulário é o da escada** (`agents_runtime.dispatch.ladder.DENIAL_REASONS`): `suppressed_block \| suppressed_silence \| suppressed_optout \| quota_exceeded \| stale_newer_message \| stale_order_paid \| rate_limit_24h \| funnel_cooldown_72h \| channel_paused_tier`, mais `manual` (cancelamento por operador, que a escada não produz). Os quatro valores antigos (`replied \| paid \| suppressed \| manual`) não cabiam os nove motivos, e achatá-los apagaria a métrica "toques cancelados **por motivo**" — que é a que diagnostica um funil |
| sent_at | timestamptz nullable | instante em que o toque foi **comprometido na outbox** (a entrega acontece depois, no sender). É o que a janela de 72h entre funis distintos mede |
| outbox_id | uuid FK → message_outbox nullable | ON DELETE SET NULL — drenar a outbox não pode apagar o fato de que um contato foi tocado |
| claimed_by / claimed_at | uuid / timestamptz nullable | quem reivindicou o toque na varredura de 1 min (`internal.claim_due_touches`) e quando. Existem para diagnóstico: toque parado em `enqueued` é o modo de falha deste passo, e "qual passe o pegou, a que horas" é a investigação inteira |
| created_at | timestamptz | CHECKs de estado inteiro: `(status='sent') = (sent_at IS NOT NULL)` e `(status='cancelled') = (cancel_reason IS NOT NULL)` (nota 9) |

### 5.6 `funnel_conversions` — receita recuperada como fato gravado (E3 · D8)
| Atributo | Tipo | Regras / descrição |
|---|---|---|
| id | uuid PK | |
| tenant_id | uuid FK | index (tenant_id, attributed_at desc) |
| funnel_id | uuid FK nullable | ON DELETE SET NULL |
| contact_id | uuid FK nullable | ON DELETE SET NULL — a purga por contato corta a pessoa e deixa o dinheiro, que é o que a LGPD pede |
| scheduled_touch_id | uuid FK nullable | ON DELETE SET NULL — qual toque levou à conversão |
| order_id | uuid FK nullable | **UNIQUE** — um pagamento credita um funil só; duas linhas dobrariam o único número pelo qual o lojista compra o produto |
| amount | numeric(12,2) NOT NULL | copiado, não juntado: o pedido pode sumir e o valor não pode |
| currency | text NOT NULL DEFAULT 'BRL' | ISO-4217 (CHECK) |
| attributed_at | timestamptz NOT NULL DEFAULT now() | pedido pago dentro de `tenants.attribution_window_hours` depois de um toque enviado |
| created_at | timestamptz | |

> **Por que a tabela existe.** `messages` tem TTL rolante de 12 meses e a conversa pode ser apagada; recalcular a atribuição depois é impossível. Toda FK é `ON DELETE SET NULL` pelo mesmo motivo: o que aconteceu tem que sobreviver ao desaparecimento daquilo a que aconteceu.

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
7. **O teto de 4 toques proativos por contato/24h é CHECK, não convenção** (RF-034 / E3 · D1): `tenants.proactive_max_per_contact_24h CHECK (BETWEEN 1 AND 4)`. Nenhum caminho de escrita, em nenhuma camada, consegue passar de 4 — e afrouxar dentro da faixa só acontece por `internal.set_proactive_cap()` (SECURITY DEFINER, EXECUTE revogado de PUBLIC, grava `audit_log`), com um trigger recusando qualquer alteração da coluna que não venha de lá. O privilégio sozinho não bastaria: um `GRANT UPDATE ON tenants` para a tela de configurações do E5 devolveria a coluna ao lojista sem nenhum teste ficar vermelho.
8. **Índice único parcial** garantindo um único `funnels.enabled = true` por (tenant, ocasião) — mesmo dispositivo da nota 1.
9. **Estados pela metade proibidos por CHECK** em `scheduled_touches`: toque `sent` sem `sent_at` some do cooldown de 72h; toque `cancelled` sem `cancel_reason` continua movendo o total e destrói a única métrica que o diagnostica.
10. `funnel_conversions` **não é arrastada por purga nenhuma** (FKs `ON DELETE SET NULL`): receita recuperada tem que sobreviver ao TTL de `messages` e à purga por contato.

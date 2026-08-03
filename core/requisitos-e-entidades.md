# Requisitos e Entidades — Plataforma de Agentes WhatsApp para E-commerce

**Versão:** 1.2 · **Data:** 2026-08-01 · **Base:** entrevista de descoberta + Arquitetura v1.3
Convenção: RF-xxx = funcional · RNF-xxx = não-funcional. Tudo aqui é rastreável a uma decisão da descoberta ou a um ADR da arquitetura.

---

## 1. Requisitos Funcionais

### 1.1 Onboarding e provisionamento

- **RF-001** O admin cadastra um cliente (nome + negócio) e gera um link único de formulário, definindo nesse momento se a conexão do número oficial será feita pelo admin ou pelo próprio cliente.
- **RF-002** O formulário coleta as respostas da taxonomia de features (identidade, objetivo, módulos de intent, política de preço, fonte de catálogo, apresentação de produto, escalonamento, agendamento, base de conhecimento) e deve ser à prova de resposta ruim (validações, exemplos, opções pré-prontas).
- **RF-003** Dentro do formulário, antes de existir conta, o cliente conecta a loja via OAuth (Shopify, Nuvemshop ou Yampi) e o WhatsApp (Cloud API oficial ou Evolution não-oficial).
- **RF-004** A conexão via API não-oficial exige aceite explícito do risco de banimento, registrado em auditoria.
- **RF-005** Um agente gerador transforma as respostas do formulário em prompt na arquitetura em camadas e cria a versão inicial do agente (draft), já conectado ao número porém **pausado**.
- **RF-006** Gate duplo de ativação: o admin testa na conta admin e aprova → o cliente testa em cenários específicos, aponta ajustes e aprova → só então o agente começa a rodar. A tela de revisão mostra prompt gerado, cenários e scores.
- **RF-007** Ao final da conexão, o cliente informa o e-mail, recebe link para criar senha e acessa o hub.
- **RF-008** Todo tenant novo entra em shadow de estreia por 7 dias: 100% das respostas avaliadas + fila de acompanhamento para o admin, sem reter envio.

### 1.2 Agente de conversação

- **RF-010** O agente responde mensagens inbound 24/7, montando o prompt em camadas: prompt-base + prompt de cenário por ocasião (PIX não pago, checkout abandonado, contato direto, etc.) + contexto do cliente injetado (total de compras, ticket médio, primeira compra, evento, mensagens enviadas) + tools + base de conhecimento.
- **RF-011** Rajadas de mensagens do mesmo contato são coalescidas (debounce de 10s, prazo empurrado a cada mensagem nova) e respondidas como um conjunto único, na ordem correta.
- **RF-012** Respostas saem com delay humanizado e indicador "digitando".
- **RF-013** O agente tem idioma principal definido pelo tenant, mas se adapta ao idioma do contato.
- **RF-014** "Nunca dizer que é IA" é configuração por tenant.
- **RF-015** Toda resposta passa pelo Judge 1 pré-envio; reprovação regenera a resposta. Violação crítica detectada após o envio dispara auto-correção na própria conversa + alerta in-app ao admin.
- **RF-016** Takeover humano: um atendente assume a conversa e o agente pausa nela; enquanto pausado, observa em silêncio (registra mensagens e atualiza slots); o retorno é somente manual ("devolver para IA"), retomando com contexto.
- **RF-017** O tenant escolhe as tools do agente dentro de um leque: consultar pedido, consultar produtos, rastreio, salvar contato, chamar humano, agendar.
- **RF-018** Follow-up proativo dentro da conversa é opcional e configurável por tenant.
- **RF-019** Escalonamento para humano envia payload da conversa pelo canal configurado. Campos sensíveis (CPF, nascimento): decisão de produto pendente — default é não coletar/persistir (repasse sem armazenamento); se a coleta for confirmada, armazenamento sob envelope encryption conforme RNF-032.
- **RF-020** Contato fora de escopo (engano/spam): o agente responde educadamente que é o WhatsApp da loja e encerra.

### 1.3 Recuperação de vendas (disparos)

- **RF-030** O sistema ingere eventos de abandono (carrinho com contato capturado, checkout, PIX não pago) via webhook das plataformas, com reconciliação por poll a cada 15 min como caminho redundante.
- **RF-031** Cada ocasião de abandono dispara um funil com cadência configurável de toques; resposta do contato encerra os toques futuros e converte em conversa normal.
- **RF-032** Antes de processar evento atrasado (> 5 min), o sistema verifica staleness: mensagem mais recente na conversa, pedido pago nesse meio-tempo ou contato em supressão abortam o disparo.
- **RF-033** Supressão de disparo: (a) todo disparo a contato novo inclui botões Autorizar/Bloquear — bloquear remove da lista; (b) silêncio após 3 disparos em funis distintos remove automaticamente; (c) opt-out expresso por mensagem, detectado pelo agente, remove. A lista de supressão é checada antes de todo envio proativo. Opt-out suprime envios, não apaga dados.
- **RF-034** Rate limits de proteção do contato: máx. 1 toque proativo/contato/24h somando todas as origens (default); teto absoluto da plataforma de 4 toques/contato/24h — afrouxar o default até o teto é ação exclusiva do admin, por tenant; o lojista só pode apertar. Não há limite de toques por funil (o total vem da cadência configurada); cooldown de 72h entre funis distintos. Mensagens reativas nunca são bloqueadas por rate limit.
- **RF-035** Envio pela Cloud API usa templates aprovados e respeita o tier de conversas/24h da Meta por número (token bucket); a 80% do tier, proativos pausam e o admin é alertado.
- **RF-036** Envio pela Evolution aplica mecanismos anti-ban: variação automática de copy por LLM a cada disparo, jitter de 30–120s entre disparos do mesmo número, warm-up de número novo com teto diário crescente e teto duro diário (default 300 proativos/dia).

### 1.4 Hub do lojista

- **RF-040** Inbox de conversas ao vivo (tempo real), com takeover e devolução para a IA.
- **RF-041** Métricas do atendimento e da recuperação: leads, agendamentos, últimos pedidos, conversões de funil.
- **RF-042** Atualização de estoque/catálogo pelos dois caminhos: upload CSV/XLSX sincronizando para tabela padronizada, e Google Sheets como fonte viva quando configurado.
- **RF-043** Pausar/despausar o agente.
- **RF-044** Editar o prompt/personalidade do agente; a edição gera nova versão que passa a valer de fato, com navegação entre versões e rollback.
- **RF-045** Visualizar o agente criado: prompt formatado, tools habilitadas, e executar testes (chat simulado + cenários sintéticos com resultado e score).
- **RF-046** Testes sazonais: conversas selecionadas apresentadas ao lojista para aprovar/desaprovar.
- **RF-047** Multi-atendente: vários logins por tenant com permissões configuráveis por papel.

### 1.5 Admin (operador da plataforma)

- **RF-050** Visão cross-tenant com logs detalhados: tool calls, latência, custo por chamada de LLM e scores dos judges — invisíveis ao lojista.
- **RF-051** Ações de operação: aprovar gates, ativar/pausar agentes, aprovar patches do flywheel.
- **RF-052** Central de alertas in-app: violação crítica, profundidade/idade de fila, mensagens em DLQ, itens unknown/failed na outbox, tier Meta próximo do limite, erro de conector.
- **RF-053** Gestão de DLQ: inspecionar e reprocessar mensagens com um clique.
- **RF-054** Purga LGPD manual por telefone: apaga conversas, contexto e embeddings do contato em todos os lugares.

### 1.6 Avaliação e melhoria contínua

- **RF-060** Cenários sintéticos: packs base por vertical mantidos pelo admin + variações geradas por IA a partir das respostas do tenant; execução produz resultado e score.
- **RF-061** Judges assíncronos avaliam conversas reais em melhor esforço e registram scores.
- **RF-062** Flywheel: a partir dos scores, o sistema propõe patches de prompt que viram novas versões (origem: flywheel) e só ativam após aprovação do admin — quanto mais o agente roda, melhor fica, com auditoria de cada mudança.

### 1.7 Dados, retenção e ciclo de vida

- **RF-070** Sync periódico de pedidos, clientes e produtos das plataformas conectadas para injeção de contexto.
- **RF-071** Retenção rolante: cada mensagem expira 12–24 meses (config do tenant) após sua data; purga diária.
- **RF-072** Cancelamento de lojista: exclusão total (hard delete) em 10 dias, sem cópia integral. A retenção anonimizada para treino/benchmark fica SUSPENSA até o ADR de uso secundário (LGPD: remover nome/telefone/CPF não anonimiza; risco de reidentificação por pedidos, cidade, datas). Interinamente, avaliação usa apenas cenários sintéticos, conversas selecionadas manualmente e desidentificadas, e métricas agregadas.
- **RF-073** Limites por tenant (disparos/mês, concorrência, features): enforcement points prontos no código com default ilimitado, para integração rápida quando a regra de planos existir.

---

## 2. Requisitos Não-Funcionais

### 2.1 Disponibilidade e durabilidade

- **RNF-001** A ingestão de webhooks é de alta disponibilidade e independente da VPS. Garantia verificável: nenhum evento é perdido após a confirmação da ingestão; eventos ausentes são reconciliados quando a API de origem permite (abandonos/pedidos por poll; inbound de WhatsApp sem webhook entregue não tem replay — limitação documentada). Perder um PIX abandonado no pico da BF é perder a venda que o produto promete recuperar.
- **RNF-002** A VPS pode falhar como ponto único do processamento: eventos acumulam e são drenados na volta, com staleness check — degradação é latência, nunca perda.
- **RNF-003** RPO próximo de zero para histórico de conversas: PITR habilitado + pg_dump periódico para storage externo + migrations reversíveis + restore sempre para projeto novo + runbook testado trimestralmente (RTO medido no drill).
- **RNF-004** Atendimento 24/7 sem janela de manutenção que interrompa respostas inbound.

### 2.2 Confiabilidade

- **RNF-010** Envio confiável: nenhuma mensagem perdida; duplicidade minimizada por outbox transacional, deduplicação interna, lease no estado `sending` e reconciliação conservadora de `unknown` (nunca reenviar cego; sem evidência → revisão manual). Risco residual de duplicata documentado e aceito — a Cloud API não tem idempotência comprovada no endpoint de mensagens; correlação de status via `biz_opaque_callback_data`.
- **RNF-011** Ingestão idempotente: o mesmo webhook processado N vezes produz exatamente 1 efeito — ON CONFLICT por chave natural incluindo a conta de origem, UNIQUE (source, source_account_id, external_event_id), mesmo caminho para webhook e reconciliação; IDs sequenciais por loja nunca colidem entre tenants.
- **RNF-012** Ordenação por conversa: lease de processamento + compare-and-set estendido na conclusão (token + version + generation + next_inbound_seq = target_seq) + `seq` por contadores atômicos (`UNIQUE conversation_id+direction+seq`) + coalescência com `processing_generation`; mensagem chegando durante a geração do LLM invalida o draft (a resposta seguinte cobre o conjunto completo); redelivery de job pelo pgmq é arquivado sem reprocessar (target_seq <= last_processed_seq); lease expirada → draft descartado, nunca enviado.
- **RNF-013** Nenhuma mensagem envenenada trava o sistema: retry com backoff exponencial + jitter, limite de tentativas por classe, DLQ por fila com alerta.
- **RNF-014** Transações sempre curtas: nenhuma transação de banco permanece aberta durante chamadas de LLM, tools ou APIs externas (claim e conclusão são updates de milissegundos).

### 2.3 Desempenho e escalabilidade

- **RNF-020** Endpoint de webhook: p99 < 500 ms sob 50 eventos/s.
- **RNF-021** Capacidade: baseline de 1.500–3.500 eventos/dia com folga; absorver rajadas intra-hora de 20–50x o baseline (todos os tenants simultaneamente) sem perda, degradando apenas a latência dos proativos.
- **RNF-022** Priorização sem starvation: weighted polling 8 (inbound) : 4 (domain events) : 2 (scheduled) : 1 (evals) com empréstimo de slots ociosos + promoção por idade (domain event > 2 min sobe a peso de inbound) — inbound tem latência mínima e pagamentos/cancelamentos de funil nunca são adiados indefinidamente.
- **RNF-023** Isolamento de vazão: teto de concorrência por tenant (default 3) — tenant em burst não afoga os demais. Premissa do MVP: runtime é um único processo asyncio (semáforo em memória é correto); multi-processo/2ª VPS exige semáforo distribuído (lease em Postgres/Redis) antes de escalar.
- **RNF-024** Workers e senders são stateless; replicação horizontal (2ª VPS nas mesmas filas) está prevista, condicionada à migração do semáforo por tenant para mecanismo distribuído.
- **RNF-025** Latência de resposta do agente: o delay humanizado é parte do produto; alvo operacional de p95 < 60s entre o fim do debounce e a gravação na outbox em operação normal.

### 2.4 Segurança

- **RNF-030** Isolamento multi-tenant em profundidade: RLS em todas as tabelas de negócio, valendo para o JWT do hub **e** para o role do runtime (`runtime_role` + `SET LOCAL app.tenant_id`); service role não circula na aplicação.
- **RNF-031** Tabelas internas (outbox, filas, evals) fora do schema exposto pela Data API. Todo acesso cross-tenant (polling de outbox, filas, purgas, reconciliação, administração) ocorre exclusivamente via funções SECURITY DEFINER de claim (ex.: claim_outbox_batch com FOR UPDATE SKIP LOCKED), sem SELECT geral para roles da aplicação.
- **RNF-032** Segredos (tokens OAuth, chaves de API, credenciais de canal) no Vault, acessados só por funções escopadas. PII sensível (CPF, nascimento): default é NÃO coletar na vertical e-commerce; se coletada, envelope encryption (chave de dados por tenant cifrada por KMS, rotação, auditoria de decifração), decifrável apenas pelo fluxo de escalonamento; chave em env var só em desenvolvimento.
- **RNF-033** Views do hub com `security_invoker`; lint no CI proíbe SQL fora da camada de repositório; suíte de vazamento cross-tenant roda com ambas as credenciais.
- **RNF-034** Autenticação do hub via Supabase Auth (e-mail + senha criada por link); autorização por membership + role.

### 2.5 Privacidade e conformidade (LGPD)

- **RNF-040** Papéis definidos: o lojista é controlador, a plataforma é operadora dos dados dos consumidores finais.
- **RNF-041** Minimização temporal: TTL rolante de 12–24 meses por mensagem com purga automática.
- **RNF-042** Direito de exclusão executável: purga por telefone cobre conversas, contexto e embeddings.
- **RNF-043** Nenhum uso secundário de conversas (treino/benchmark) antes do ADR próprio com teste de risco de reidentificação; purga de lojista em 10 dias é hard delete.
- **RNF-044** Consentimento e oposição registrados: botões Autorizar/Bloquear, supressões com motivo e timestamp, aceite do risco Evolution em auditoria.

### 2.6 Observabilidade e auditabilidade

- **RNF-050** Toda chamada de LLM e de tool registra latência, custo e resultado; toda resposta registra score do Judge 1.
- **RNF-051** Métricas operacionais contínuas: profundidade e idade das filas, DLQs, estados da outbox, tier Meta por número, erros de conector — com alerta in-app.
- **RNF-052** Auditoria completa de mudanças: versões de agente append-only com autor/origem/diff; ações do hub em audit_log.

### 2.7 Manutenibilidade e evolução

- **RNF-060** Fronteiras de módulo verificadas por teste no CI (fitness functions): channels não importa connectors, etc.
- **RNF-061** Canais e conectores são portas com adaptadores: adicionar e-mail/Instagram DM ou nova plataforma não refatora o núcleo; disparos cross-channel operam por contato, não por número.
- **RNF-062** Diferença entre tenants é exclusivamente config/dados; um deploy atualiza todos.
- **RNF-063** Gatilhos de evolução documentados (fila > ~50 msg/s → Redis/SQS; starvation → claim balanceado; saturação → 2ª VPS; analytics → read replica) — decisões futuras já têm critério.

---

## 3. Entidades do sistema

Agrupadas por domínio. Todas as de negócio carregam `tenant_id` + RLS.

### 3.1 Identidade e acesso

| Entidade | O que representa | Atributos-chave | Relações |
|---|---|---|---|
| **Tenant** | O lojista/loja cliente da plataforma | status, active_version_id, retenção (meses), flag "nunca dizer que é IA", follow-up proativo on/off | 1:N com quase tudo |
| **User** | Pessoa que acessa o hub ou o admin | e-mail, credenciais (Supabase Auth) | N:M com Tenant via Membership |
| **Membership** | Vínculo usuário × tenant com papel | role, permissões | User ↔ Tenant |
| **AuditLog** | Ação relevante no hub/admin | ator, ação, alvo, timestamp, payload (inclui aceite de risco Evolution) | → User, Tenant |

### 3.2 Agente e configuração

| Entidade | O que representa | Atributos-chave | Relações |
|---|---|---|---|
| **AgentVersion** | Versão imutável (append-only) da config do agente | prompt-base, prompts de cenário, tools habilitadas, identidade/tom, autor, origem (bruno\|lojista\|flywheel), parent_version_id, status (draft\|active\|archived) | → Tenant; auto-referência (parent) |
| **OnboardingInvite** | Convite/link de formulário gerado pelo admin | token, quem conecta o número (admin\|cliente), status | → Tenant |
| **FormResponse** | Respostas do formulário de onboarding | respostas da taxonomia, anexos de estoque | → OnboardingInvite; insumo do AgentVersion inicial |
| **Scenario** | Cenário de teste sintético | pack base + variações geradas por IA, entrada esperada | → Tenant; usado por EvalRun |
| **KnowledgeChunk** | Trecho da base de conhecimento (FAQ/políticas) com embedding | conteúdo, embedding (pgvector), fonte | → Tenant |

### 3.3 Canais e conectores

| Entidade | O que representa | Atributos-chave | Relações |
|---|---|---|---|
| **ChannelAccount** | Número/conta de WhatsApp conectada | tipo (cloud\|evolution), ref. de credencial no Vault, tier Meta, estado de warm-up, teto diário | → Tenant |
| **ConnectorAccount** | Loja conectada (plataforma) | plataforma (shopify\|nuvemshop\|yampi), ref. OAuth no Vault, estado do sync | → Tenant |
| **Order / Customer / Product** | Espelho sincronizado dos dados da loja para contexto e tools | chaves externas da plataforma, campos de contexto (total de compras, ticket médio...) | → Tenant, ConnectorAccount |

### 3.4 Conversação

| Entidade | O que representa | Atributos-chave | Relações |
|---|---|---|---|
| **Contact** | Pessoa do outro lado do WhatsApp | telefone normalizado, nome, idioma detectado, opt-in/out | → Tenant; 1:N Conversations |
| **Conversation** | Uma conversa com um contato | estado (ia\|humano\|encerrada), ocasião de origem, slots, pending_response_at (debounce), last_processed_seq, next_inbound_seq/next_outbound_seq (contadores atômicos), processing_generation, processing_token/processing_until (lease), version (CAS) | → Contact, Tenant |
| **Message** | Mensagem individual | direção, conteúdo, canal, seq — UNIQUE (conversation_id, direction, seq), expires_at (TTL rolante) | → Conversation |
| **SuppressionEntry** | Contato que não recebe proativos | motivo (bloqueio\|silêncio pós-3\|opt-out por intenção), timestamp | → Contact, Tenant |

### 3.5 Disparos e recuperação

| Entidade | O que representa | Atributos-chave | Relações |
|---|---|---|---|
| **WebhookEvent** | Evento bruto recebido (loja/canal) | fonte, source_account_id, external_event_id — UNIQUE (source, source_account_id, external_event_id), payload, status de processamento | origem de tudo; → Tenant (resolvido pela conta de origem) |
| **Funnel** | Configuração de funil por ocasião | ocasião (pix\|checkout\|carrinho), cadência de toques, copy/templates, canal | → Tenant |
| **ScheduledTouch** | Toque futuro agendado de funil/follow-up | due_at, funil, contato, nº do toque, status | → Funnel, Contact |
| **MessageOutbox** | Intenção de envio (transactional outbox) | payload, idempotency_key (enviada em biz_opaque_callback_data), status (pending\|sending\|sent\|failed\|unknown), attempt_count, provider_message_id, locked_by/locked_until (lease do sending), next_attempt_at, last_error, request_started_at, payload_hash | → Conversation, ChannelAccount |
| **Filas pgmq** (infra) | q_inbound, q_domain_events, q_scheduled, q_evals + DLQs | visibility timeout, read_ct | transportam refs de WebhookEvent/Conversation |

### 3.6 Avaliação e observabilidade

| Entidade | O que representa | Atributos-chave | Relações |
|---|---|---|---|
| **EvalRun** | Execução de cenários sintéticos | resultado, score agregado, versão testada | → Scenario, AgentVersion |
| **JudgeScore** | Avaliação de uma resposta/conversa | judge (pré-envio\|assíncrono), score, veredito, justificativa | → Conversation/Message |
| **ToolCall** | Chamada de tool executada pelo agente | tool, input/output, latência, erro | → Conversation, Message |
| **LlmCall** | Chamada de LLM | provider, modelo, tokens, custo, latência | → Conversation/EvalRun |
| **Alert** | Alerta in-app do admin | tipo (violação\|fila\|DLQ\|outbox\|tier\|conector), severidade, status | → Tenant (quando aplicável) |

### 3.7 Futuro próximo (placeholders já previstos)

| Entidade | O que representa | Observação |
|---|---|---|
| **QuotaRule** | Regra de limite por plano/tenant | Enforcement points já existem no código; entidade nasce quando a regra de planos for definida (RF-073) |
| **Channel: email / instagram_dm** | Novos adaptadores de canal | Roadmap 12–24 meses; Contact já é a chave cross-channel |

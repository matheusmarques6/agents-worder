# Testes por Nível e Mapa de CI/CD

**Versão:** 1.1 · **Data:** 2026-08-01 · **Base:** Plano de Testes v1.0 · Arquitetura v1.3
**Changelog v1.1:** notação de requisitos por extenso; padronização de terminologia em português; redação formalizada; gate de release amarrado ao mesmo commit; critérios quantitativos de carga; política de cassetes; definição precisa do E2E; glossário; sete coberturas adicionais (observabilidade, JWT, rotação de segredos, isolamento de dados de teste, acessibilidade, política de instáveis, compatibilidade N-1).

Este documento operacionaliza o plano: o que é unitário, o que é integração, o que é E2E — e exatamente o que roda em cada ocasião do CI/CD.

---

## 0. Glossário

- **BF:** Black Friday — cenário de pico dimensionante (rajada de 20–50x a taxa por minuto do baseline).
- **CAS:** compare-and-set — atualização condicionada ao estado atual da linha; falhou a condição, nada muda.
- **VT (visibility timeout):** tempo de invisibilidade de uma mensagem lida da fila pgmq; sem arquivamento, ela reaparece.
- **PITR:** point-in-time recovery — recuperação do banco para um instante específico.
- **RLS tripla:** suíte de vazamento cross-tenant executada com as três credenciais (JWT de usuário, `worker_role`, `sender_role`).
- **Judge 1:** avaliador síncrono pré-envio; toda resposta do agente passa por ele antes de sair.
- **Shadow:** os 7 primeiros dias de um tenant novo, com 100% das respostas avaliadas e fila de acompanhamento, sem reter envio.
- **Warm-up:** aquecimento de número Evolution com teto diário crescente de envios proativos.
- **`unknown` / `manual_review`:** estados da outbox — envio sem confirmação de resultado / item aguardando decisão humana.
- **Cassete:** gravação de requisição/resposta real de uma API externa, usada como simulação determinística nos níveis inferiores.
- **DLQ:** dead-letter queue — fila de mensagens que esgotaram as tentativas ou falharam de forma permanente.

---

## 1. Critério de classificação (a regra que decide onde cada teste vive)

| Nível | Toca o quê | O que é simulado | Velocidade | Marcador |
|---|---|---|---|---|
| **Unitário** | nada de E/S: sem Postgres, sem rede, sem sistema de arquivos | tudo (LLM, repositórios, relógio) | ms — suíte inteira < 60s | `@pytest.mark.unit` |
| **Integração-DB** | Postgres real (local) — funções SQL, RLS, triggers | runtime parcial; sem LLM/APIs | segundos | `@pytest.mark.db` / `@pytest.mark.rls` |
| **Integração-pipeline** | runtime real + Postgres + pgmq | LLM (respostas fixas) e APIs externas (cassetes) | segundos a minutos | `@pytest.mark.pipeline` |
| **Contrato** | UMA API externa real (Meta, Evolution, plataformas, rastreio) | todo o resto | minutos, instável por natureza | `@pytest.mark.contract` |
| **E2E** | sistema completo em staging (hub + edge functions + runtime + Supabase) | **sem simulações internas**; integra-se a contas e ambientes externos de teste previamente configurados (números WhatsApp de teste, lojas de desenvolvimento, sandbox de rastreio) | minutos por jornada | Playwright (repositório do hub) |

Regra prática ao escrever um teste novo: se dá para testar sem Postgres, é unitário; se precisa de Postgres mas não do laço do runtime, é DB; se precisa do laço (filas, coalescer, senders), é pipeline; se precisa de terceiro real, é contrato; se precisa de navegador ou WhatsApp de verdade, é E2E. Sempre empurre para o nível mais baixo possível.

---

## 2. Testes unitários (por módulo)

**Ferramentas:** pytest + fábricas de dados; relógio congelado (`freezegun`); LLM = dublê que devolve resposta fixa.

| Módulo | Casos principais |
|---|---|
| `queueing` | cálculo do backoff exponencial e limites do jitter; classificação de erro transitório × permanente (timeout/429/5xx vs. payload inválido/credencial revogada); decisão do weighted polling 8:4:2:1 dado o estado das filas; decisão de promoção por idade (evento de domínio > 2 min, agendado > 10 min); semáforo por tenant (aquisição, liberação, teto 3, tenant no limite → decisão de `set_vt`) |
| `agent_core` | montagem do prompt em camadas na ordem correta (base → cenário por ocasião → contexto do cliente → tools → base de conhecimento); seleção do prompt de cenário por `origin_occasion`; injeção dos campos de contexto (total de compras, ticket médio, primeira compra); adaptação de idioma; respeito ao `never_say_ai`; think-gate |
| `dispatch` | supressão pelos 3 motivos; limites de proteção (1 proativo/contato/24h somando origens como default, teto da plataforma de 4/24h — afrouxar é só do admin, lojista só aperta; intervalo mínimo de 72h entre funis; mensagem reativa nunca bloqueada); decisão de obsolescência de evento (mensagem posterior / pedido pago / contato suprimido); cálculo de cadência do funil; seleção de canal (`cloud \| evolution \| auto`) |
| `channels` | variação do texto da mensagem nunca repete a última do mesmo número; jitter dentro de 30–120s; tetos de warm-up por estágio (20→50→100); token bucket do tier Meta com pausa de proativos a 80%; montagem de payload por adaptador (template + botões Autorizar/Bloquear em contato novo); cálculo do atraso humanizado |
| `judges` | interpretação da rubrica; mapeamento pontuação → `pass \| fail \| critical`; fluxo de decisão regenerar × auto-corrigir × alertar |
| `parsers` | payload bruto de cada fonte (shopify, nuvemshop, yampi, meta, evolution) → evento normalizado; extração de `source_account_id` e `external_event_id` por fonte |
| `quota` | ponto de aplicação com regra fictícia (bloqueia/permite/alerta); valor padrão ilimitado |
| `onboarding` | validações do formulário (à prova de resposta ruim); modelo do gerador de prompt preenchido a partir de `answers` |

**Meta de tempo:** suíte completa < 60s. **Ocasião:** todo push de PR.

---

## 3. Testes de integração

### 3.1 Integração-DB (`-m db`) — Postgres real via Supabase CLI local
1. `ingest_webhook`: atomicidade (erro no meio → nada persiste, nem evento nem item de fila); reprocessamento triplo do mesmo webhook → exatamente 1 efeito; colisão de `external_event_id` entre duas lojas (contas de origem distintas) → 2 eventos distintos; mesma loja → `duplicate`; resolução do tenant pela conta de origem; o ramo inbound grava `messages` + `seq` + `pending_response_at` e **não** enfileira; o ramo de plataforma enfileira em `q_domain_events`.
2. Contadores de `seq`: duas conexões concorrentes recebem sequências distintas e consecutivas; a violação de `UNIQUE (conversation_id, direction, seq)` é impossível pelo caminho oficial.
3. CAS estendido: cada condição falhando isoladamente (token errado; `version` divergente; `generation` obsoleta; `next_inbound_seq > target_seq`) → 0 linhas afetadas; caminho feliz → conclusão + outbox na mesma transação; liberação da lease apenas com o token do dono.
4. Transação do coalescer: reversão simulada → `pending_response_at` intacto e nenhum job; sucesso → `generation++` + job + campo limpo, tudo ou nada.
5. `claim_outbox_batch`: dois consumidores simultâneos recebem partições disjuntas (`FOR UPDATE SKIP LOCKED`); respeita `p_limit`; retorna apenas as linhas atribuídas; não aceita filtros arbitrários.
6. Funções de segredo: `get_channel_secret` executável por `sender_role` e negada a `worker_role` (e o inverso para a de conector); `search_path` fixo; tenant/conta inválidos → erro; EXECUTE revogado de PUBLIC.
7. Triggers e restrições: `expires_at` preenchido a partir de `retention_months`; índice parcial garantindo um único `agent_versions.status='active'` por tenant; cascatas de purga arrastando `judge_scores`, `tool_calls` e `llm_calls`.
8. **Compatibilidade N-1 (nova):** a versão anterior do runtime executa contra o esquema expandido pela migration nova (fase expand) sem erro — pré-condição para autorizar a fase de remoção (contract) em release posterior.

### 3.2 Suíte de segurança RLS (`-m rls`) — **bloqueante em todo PR**
Para cada tabela de negócio: leitura e escrita cross-tenant tentadas com (a) JWT do usuário do tenant A contra dados do B, (b) `worker_role` com `app.tenant_id` do A, (c) `sender_role` idem → **qualquer linha retornada ou afetada reprova a suíte**. Complementos: verificações de que `worker_role`/`sender_role` não possuem `BYPASSRLS` nem a propriedade das tabelas protegidas; views do hub com `security_invoker`; tabelas internas invisíveis pela Data API.
**Coberturas adicionais (novas):**
- **JWT:** token expirado, revogado, malformado e com claims incompletas → acesso negado em todos os casos, sem erro 500.
- **Rotação de segredos:** troca de credencial no Vault → novo valor passa a funcionar sem indisponibilidade; o valor antigo é recusado; a rotação fica registrada.

### 3.3 Integração-pipeline (`-m pipeline`) — runtime completo, LLM e APIs simulados
1. Fluxo inbound feliz: 5 mensagens em rajada → debounce → 1 job → 1 chamada de LLM → Judge 1 → outbox → sender simulado → `sent`.
2. **Invariante central:** mensagem injetada durante a FASE 2 → CAS falha → rascunho descartado → lease liberada → o novo job responde ao conjunto completo (e somente ele).
3. Reentrega pelo pgmq (job não arquivado retorna) → as validações do worker arquivam sem segunda chamada de LLM (`target_seq <= last_processed_seq`).
4. Encerramento abrupto do processo entre o claim e o envio do coalescer → após a reinicialização, nenhuma conversa órfã e nenhum job duplicado.
5. Lease expira no meio da FASE 2 → um segundo worker assume → o CAS do primeiro falha → nada é enviado em duplicidade.
6. Heartbeat: processamento longo simulado → VT renovado → sem reentrega durante o trabalho.
7. Mensagem envenenada → backoff → limite de tentativas → DLQ da fila correta + alerta criado; o reprocessamento manual conclui o tratamento e remove o item da DLQ.
8. Weighted polling sob mistura de filas: proporção 8:4:2:1 observada; um evento de domínio com mais de dois minutos recebe prioridade sobre a fila de entrada (promoção por idade).
9. Semáforo: um tenant que atingiu o limite de processamento simultâneo tem suas mensagens devolvidas (`set_vt`) enquanto os demais tenants seguem fluindo.
10. Outbox: encerramento abrupto do processo de envio durante uma operação em andamento → item em `unknown` → **nenhum reenvio**; status webhook correlacionado (wamid/`biz_opaque_callback_data`) → `sent`; janela sem evidência → `manual_review` + alerta.
11. Dispatch: `order_paid` cancela os `scheduled_touches` pendentes; supressão bloqueia o proativo; evento obsoleto é descartado; o funil completo respeita o limite por contato/24h vigente, a cadência configurada e o intervalo entre funis.
12. Takeover: conversa em estado `humano` → o agente registra e atualiza slots, sem responder; "devolver para IA" reativa com contexto.
13. Purgas: TTL remove apenas mensagens vencidas; purga de lojista (10 dias) → exclusão definitiva completa; purga por contato → conversas + contexto + embeddings derivados zerados.
14. **Observabilidade (nova):** todo erro crítico simulado (violação do Judge, item em DLQ, `manual_review`) produz simultaneamente log estruturado, métrica e alerta, todos com o mesmo identificador de correlação — testado por asserção nos três destinos.
15. **Isolamento de dados de teste (nova):** cada execução usa tenants sintéticos com prefixo único da execução; ao final, a limpeza os remove; duas execuções paralelas não colidem.

### 3.4 Contrato (`-m contract`) — API externa real, staging, execução semanal ou manual
Cloud API (envio de template, botões, recepção de status webhook, ida e volta do `biz_opaque_callback_data`, **teste de duplicidade em reenvio** — resolve a pendência nº 2 da arquitetura); Evolution (envio/recepção); Shopify, Nuvemshop e Yampi (OAuth, registro de webhook, evento real de teste, poll de reconciliação); API de rastreio.

**Política de cassetes (nova):**
- Cassetes são gerados pelas execuções de contrato, **versionados no repositório** e revisados em PR como qualquer código.
- **Validade máxima: 30 dias.** Cassete vencido faz a execução noturna emitir aviso e agendar a suíte de contrato correspondente; não reprova sozinho.
- A atualização de um cassete **nunca altera suítes bloqueantes automaticamente**: entra por PR normal, que roda toda a esteira — se o novo comportamento do provedor quebra um teste, a quebra aparece no PR do cassete, com o Bruno como aprovador.

---

## 4. Testes E2E — Playwright + staging completo

Sem simulações internas: as jornadas integram contas e ambientes externos de teste previamente configurados (números WhatsApp de teste, lojas de desenvolvimento nas três plataformas, sandbox da API de rastreio).

| Jornada | Cobre |
|---|---|
| Onboarding completo: admin gera convite → formulário → OAuth na loja de desenvolvimento → conexão do WhatsApp de teste → gerador cria versão rascunho pausada | RF-001 a RF-005 |
| Gate duplo: admin testa e aprova → cliente testa, aponta ajuste e aprova → ativação + shadow ligado | RF-006 e RF-008 |
| Criação de conta: e-mail → link de senha → primeiro acesso ao hub | RF-007 |
| Recuperação ponta a ponta: checkout abandonado sintético na loja de desenvolvimento → toque chega no WhatsApp de teste → resposta do contato → funil cancelado → conversa com o agente → conversão | RF-030 a RF-035 |
| Inbox em tempo real: mensagem aparece ao vivo → takeover → agente em modo observador → devolver para IA | RF-016 e RF-040 |
| Versões: lojista edita o prompt → nova versão passa a valer de fato → navegação entre versões → reversão em 1 clique | RF-044 |
| Estoque: upload CSV/XLSX → tabela padronizada; troca para Google Sheets como fonte viva | RF-042 |
| Permissões: `attendant` não executa ações exclusivas de `owner`; múltiplos logins simultâneos | RF-047 |
| Operação: pausar/despausar; testes sazonais (aprovar/desaprovar conversas selecionadas) | RF-043 e RF-046 |
| Admin: central de alertas; DLQ com reprocessamento em 1 clique; logs de custo/latência/judges invisíveis ao lojista | RF-050 a RF-053 |
| **Acessibilidade (nova):** verificações básicas nas telas principais do hub — navegação por teclado, ordem de foco, rótulos de formulário e contraste (Playwright + axe-core) | RNF transversal |

**Ocasião:** subconjunto de fumaça no deploy de staging; suíte completa na execução noturna.

---

## 5. Suítes especiais (fora da pirâmide, com agenda própria)

| Suíte | O que é | Critérios de aprovação | Quando roda |
|---|---|---|---|
| **Carga/resiliência** | rajada de 20–50x por 1h, 25 tenants sintéticos, encerramentos abruptos de processo durante a rajada | ver critérios quantitativos abaixo | mensal + obrigatória antes da BF |
| **Evals de IA** | harness de cenários + Judge com rubrica + testes adversariais básicos (injeção via mensagem do contato, tentativa de revelar o prompt, tentativa de forçar "sou uma IA" quando configurado o contrário) | pontuação agregada ≥ limite mínimo definido por rubrica; zero veredito `critical` | gate de ativação de **cada versão de agente** — não pertence ao CI de código |
| **Exercícios de restauração** | caminho PITR/projeto novo e caminho pg_dump; valida Vault legível e senhas de roles; mede o RTO real | os dois caminhos concluídos; segredo do Vault lido após a restauração; RTO registrado | trimestral |
| **Fumaça de produção** | verificação de integridade do runtime; evento sintético via `ingest_webhook`; envio real a número interno; leitura de segredo | tudo verde em ≤ 5 min | após todo deploy em produção |

**Critérios quantitativos da suíte de carga (aprovação exige todos):**
1. Perda de eventos = **0**; duplicidade de envio = 0 sem passar por `manual_review`.
2. Latência de processamento inbound (fim do debounce → gravação na outbox): **p95 ≤ 2 min, p99 ≤ 5 min** durante a rajada.
3. Proativos podem degradar até **30 min** de atraso durante a rajada (degradação aceita por projeto); nunca além.
4. Vazão sustentada: ≥ **150 eventos/min** processados no pico com as proporções de polling respeitadas.
5. Drenagem: fila zerada em **≤ 2h** após o fim da rajada.
6. Taxa de falhas permanentes < **0,5%** dos eventos; DLQ vazia ou 100% justificada na análise.
7. Recursos da VPS: CPU < **80%** sustentado, memória < **75%**, sem crescimento contínuo (vazamento).
8. Backlog: nenhum item de `q_inbound` com idade > **10 min** sem promoção/tratamento.

Estes valores são a linha de base inicial; ajustes exigem atualização deste documento no mesmo PR (fitness function de processo).

---

## 6. Mapa CI/CD — o que roda em cada ocasião

### 6.1 Tabela-mestre

| Ocasião (gatilho) | O que roda | Bloqueante? | Tempo alvo | Ambiente |
|---|---|---|---|---|
| **Todo push em PR** | lint (ruff/eslint) + lint anti-SQL fora do repositório de dados + teste de fronteiras de módulo + `unit` + `db` + `rls` | **Sim — sem merge com falha** | < 5 min | contêiner do CI (Postgres efêmero) |
| **Merge em `main`** | tudo do PR + `pipeline` completa | **Sim** | < 15 min | CI |
| **Deploy automático em staging** (após merge verde) | migrations → deploy das edge functions → deploy do runtime → subconjunto E2E de fumaça (3 jornadas: inbound, recuperação, login do hub) | Sim, para promover | ~20 min | staging |
| **Noturno** | E2E completo em staging + verificação de validade dos cassetes | Não bloqueia; abre issue automática | ~40 min | staging |
| **Semanal (agendado)** | `contract` real (Meta, Evolution, plataformas, rastreio) + proposta de atualização de cassetes via PR | Não; falha vira alerta | variável | staging + contas reais |
| **Mensal + pré-BF** | carga/resiliência com os critérios quantitativos do §5 | decisão de prosseguir/parar da BF | ~2 h | staging |
| **Trimestral** | 2 exercícios de restauração | auditoria; falha = incidente | ~meio dia | projeto de staging descartável |
| **Release em produção** (gatilho manual) | checklist do §6.2 + deploy do §6.3 + fumaça de produção | **Sim** | ~15 min | produção |
| **Por versão de agente** (ativação) | eval harness ≥ limite mínimo + gate duplo humano | **Sim — versão não ativa sem pontuação** | minutos | staging/produção (chat simulado) |

### 6.2 Gate de release para produção (checklist automatizado)
1. `main` verde nas suítes bloqueantes (unit, db, rls, pipeline) **no commit candidato**.
2. **E2E do mesmo commit/candidato a release:** o resultado E2E usado no gate deve ter sido executado contra o mesmo commit (ou tag) que será implantado. Se for reutilizado um resultado anterior (ex.: o noturno), uma verificação automatizada confirma que nenhuma alteração ocorreu depois dele em caminhos relevantes (runtime/, hub/, migrations/); qualquer diferença dispara nova execução E2E antes do deploy.
3. Zero defeitos S1/S2 abertos.
4. Migrations do release são **aditivas** (fase expand); mudanças destrutivas (fase contract) só em release próprio posterior, com o teste de compatibilidade N-1 (§3.1.8) verde e plano de reversão.
5. Para o primeiro tenant real e antes da BF: última suíte de contrato da Cloud API verde + exercício de restauração dentro do trimestre.

### 6.3 Sequência de deploy (a ordem importa)
```
1. Migrations no Supabase (apenas aditivas, reversíveis)   ← primeiro: esquema à frente do código
2. Deploy das Edge Functions de ingestão                   ← a ingestão nova já grava no esquema novo
3. Deploy do runtime na VPS:
   desligamento gracioso → para de fazer claim → conclui
   leases/VTs em andamento → troca a imagem → sobe
   (a fila absorve o intervalo; nada se perde — é o mesmo
   mecanismo da queda de VPS)
4. Hub: Vercel (preview por PR; promoção após verificações)
5. Fumaça de produção (§5)
Reversão: runtime/hub = imagem/deploy anterior;
migrations = nunca reverter em produção — apenas avançar
com correção (por isso o padrão expand-contract).
```

### 6.4 Estrutura no repositório
```
runtime/
  tests/unit/          @unit      — por módulo (§2)
  tests/db/            @db, @rls  — funções SQL, restrições, RLS tripla, N-1
  tests/pipeline/      @pipeline  — cenários 1 a 15 do §3.3 (encerramentos via harness)
  tests/contract/      @contract  — 1 arquivo por provedor; cassetes em tests/cassettes/ (validade 30 dias)
  tests/load/                     — gerador de rajada + relatório com os critérios do §5
hub/
  e2e/                 Playwright — jornadas do §4 (inclui axe-core)
.github/workflows/
  pr.yml         → unit + db + rls + lints + fronteiras
  main.yml       → pr.yml + pipeline → deploy staging → E2E de fumaça
  nightly.yml    → E2E completo + validade de cassetes
  weekly.yml     → contract (gera PRs de cassete)
  load.yml       → manual/mensal
  release.yml    → gate §6.2 + deploy §6.3 + fumaça
```

### 6.5 Regras de higiene do CI
- **Teste instável (flaky) é defeito S3 com dono e prazo:** responsável = Bruno; prazo de correção = 7 dias; até lá o teste roda em quarentena visível (não silencia). Ou estabiliza, ou desce de nível — quase sempre é possível descer para `pipeline` ou `db`.
- Nenhum teste bloqueante depende de rede externa — apenas as suítes `contract` tocam terceiros.
- O relógio é sempre injetável no runtime: debounce, obsolescência, intervalos entre funis, warm-up e TTL são testados sem espera real.
- Todo defeito de produção vira teste de regressão **no nível mais baixo que o reproduz**, no mesmo PR da correção.
- Mudou um invariante na arquitetura → a suíte correspondente muda no mesmo PR (fitness function de processo, já prevista no Plano de Testes §7).

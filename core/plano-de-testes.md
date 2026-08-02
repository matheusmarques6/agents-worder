# Plano de Testes — Plataforma de Agentes WhatsApp para E-commerce

**Versão do plano:** 1.0 · **Data:** 2026-08-01
**Baseado em:** Arquitetura v1.3 · Requisitos e Entidades v1.2 · Dicionário de Dados v1.0
**Responsável:** Bruno (dev solo + Claude Code) — o plano assume execução com automação máxima e quase nenhum teste manual repetitivo.

---

## 1. Objetivo do teste

**Objetivo geral:** provar, antes de cada tenant real entrar, que a plataforma cumpre suas quatro promessas de negócio: (1) nenhum evento perdido após a confirmação da ingestão; (2) nenhuma mensagem enviada duplicada por retry cego, desatualizada, para contato suprimido ou acima dos limites de proteção; (3) nenhuma resposta do agente sai sem passar pelo Judge 1; (4) nenhum dado de um tenant é visível a outro tenant, por nenhuma credencial.

**Objetivos específicos (mapeados aos invariantes da arquitetura):**
- O1 — Validar a atomicidade e idempotência da ingestão (`ingest_webhook`), incluindo colisão de IDs entre lojas (`source_account_id`).
- O2 — Validar o ciclo coalescer → job → lease → CAS estendido, em especial o **invariante central**: mensagem chegando durante a geração do LLM invalida o draft.
- O3 — Validar a semântica de fila (backoff, VT renovável, DLQ, weighted polling 8:4:2:1, promoção por idade, semáforo por tenant).
- O4 — Validar a outbox: nenhum reenvio cego de `unknown`, lease do `sending`, correlação por status webhook, fila de revisão manual.
- O5 — Validar as proteções de disparo: supressão (3 motivos), rate limits, staleness, cancelamento por pagamento, anti-ban, tier Meta.
- O6 — Validar o isolamento multi-tenant com as três credenciais (JWT, `worker_role`, `sender_role`) e o acesso a segredos só por funções escopadas.
- O7 — Validar a qualidade do agente por evals: cenários sintéticos com score, gate duplo, shadow de estreia.
- O8 — Validar LGPD operacional: TTL rolante, purga de lojista (hard delete), purga por contato (incl. embeddings), opt-out.
- O9 — Validar recuperação de desastre: os dois caminhos do runbook de restore, com leitura de segredo do Vault pós-restore.
- O10 — Validar capacidade: rajada Black Friday 20–50x sem perda, com prioridades respeitadas.

---

## 2. Item de teste e versão

| Item | Versão sob teste |
|---|---|
| Runtime Python (workers, coalescer, senders, scheduler, judges) | a cada release candidate; suítes completas no CI de `main` |
| Funções SQL (`ingest_webhook`, `claim_outbox_batch`, `get_*_secret`, purgas) | a cada migration |
| Edge Functions de ingestão (`/wh/{fonte}`) | a cada deploy |
| Hub Next.js (formulário, hub, admin) | a cada release candidate |
| Políticas RLS e roles (`worker_role`, `sender_role`) | a cada migration (suíte de vazamento é bloqueante) |
| Prompts/versões de agente | por eval run (gate de ativação), não por CI |

Rastreabilidade: todo caso de teste referencia RF-xxx/RNF-xxx dos Requisitos v1.2. Critério de cobertura primário: **100% dos invariantes e RNFs críticos cobertos por teste automatizado** — cobertura de linha é métrica secundária.

---

## 3. Escopo

### 3.1 Dentro do escopo
1. Ingestão: HMAC por fonte, `ingest_webhook` (atomicidade, ON CONFLICT, resolução de tenant, branch inbound × evento), p99 < 500ms @ 50 ev/s.
2. Pipeline de conversação: debounce/coalescer, validações de job (generation/target_seq/redelivery), lease + CAS estendido, contadores de `seq`, takeover/observador/devolução manual.
3. Filas: retry/backoff/jitter, VT heartbeat, DLQs + alerta + reprocesso, weighted polling, aging, semáforo por tenant (premissa de processo único).
4. Outbox e senders: `claim_outbox_batch`, estados (`pending→sending→sent/failed/unknown/manual_review`), lease do sending, reconciliação conservadora, `biz_opaque_callback_data`.
5. Dispatch: funis por ocasião, cadência, supressão, rate limits, staleness, cancelamento por `order_paid`, anti-ban Evolution, token bucket/tier Cloud.
6. Segurança: suíte de vazamento cross-tenant tripla, funções SECURITY DEFINER (search_path, EXECUTE, escopo), Vault, views `security_invoker`, lint anti-SQL ad hoc.
7. Qualidade do agente (evals): packs de cenário + variações, Judge 1 (pass/fail/critical), regeneração, auto-correção, políticas por tenant (preço, "nunca dizer que é IA", idioma, inventory hard rule, fora de escopo).
8. Onboarding E2E: convite → formulário → OAuth loja → conexão WhatsApp → gerador de prompt → gate duplo → e-mail/senha → hub → shadow.
9. Hub: inbox realtime, versões (editar/navegar/rollback), estoque (CSV/XLSX + Google Sheets), pausar/despausar, permissões multi-atendente, testes sazonais.
10. LGPD: purga TTL, purga de lojista (10 dias, hard delete), purga por contato, opt-out (3 fluxos), botões Autorizar/Bloquear.
11. Resiliência: kill do processo em pontos críticos (entre claim e send do coalescer; meio do envio do sender; meio da FASE 2), drenagem pós-queda com staleness.
12. Carga: baseline (3.500 ev/dia) e rajada BF (2–5 ev/s sustentados por 1h, 25 tenants simultâneos).
13. Restore: drill dos dois caminhos (PITR/projeto novo e pg_dump), cronometrado (mede RTO real), incluindo Vault e senhas de roles.

### 3.2 Fora do escopo (desta fase)
- Testes dos apps das plataformas (Shopify/Nuvemshop/Yampi em si) e da infra da Meta/Evolution — cobrimos só os nossos contratos com elas.
- Pentest formal externo (fica para pré-enterprise); aqui é a suíte de segurança interna.
- Testes de billing (fora da plataforma) e de quota real (só os enforcement points com regra dummy).
- Canais futuros (e-mail, Instagram DM) — apenas o teste de fronteira de módulo que garante a porta.
- Carga além de 50x e multi-VPS (o semáforo distribuído tem gatilho próprio; testar quando o gatilho disparar).
- Apps móveis/browsers exóticos: hub testado em Chrome desktop + Chrome Android (público lojista).

---

## 4. Riscos

### 4.1 Riscos do produto que o teste mitiga (priorização do esforço)
| # | Risco | Impacto | Prob. | Prioridade de teste |
|---|---|---|---|---|
| R1 | Mensagem duplicada de cobrança (PIX) por retry cego | Altíssimo (queima a marca do lojista) | Média | P0 — O4 |
| R2 | Resposta desatualizada (mensagem chegou durante o LLM) | Alto | Alta sem o CAS estendido | P0 — O2 |
| R3 | Evento perdido no pico BF (venda não recuperada) | Altíssimo | Média | P0 — O1/O10 |
| R4 | Vazamento cross-tenant (dados de uma loja em outra) | Altíssimo (mata o produto) | Baixa com RLS, mas catastrófico | P0 — O6 |
| R5 | Disparo a contato suprimido / acima do limite | Alto (compliance WhatsApp + LGPD) | Média | P0 — O5 |
| R6 | Resposta sem Judge 1 / violação crítica não detectada | Alto | Baixa | P1 — O7 |
| R7 | Starvation: pagamento não cancela funil a tempo | Alto (cobra quem pagou) | Média | P1 — O3 |
| R8 | Banimento de número Evolution por padrão de disparo | Alto | Alta por natureza | P1 — O5 |
| R9 | Purga LGPD incompleta (embeddings/derivados sobram) | Médio-alto (jurídico) | Média | P1 — O8 |
| R10 | Backup irrecuperável (Vault ilegível no restore) | Altíssimo, raro | Baixa | P1 — O9 |
| R11 | Agente ruim no onboarding (cliente perde confiança no gate) | Médio | Média | P2 — O7 |

### 4.2 Riscos do próprio processo de teste
- **Dev solo:** tentação de pular testes sob pressão de prazo → mitigação: suítes P0 são bloqueantes no CI, sem merge com falha.
- **LLM não-determinístico:** testes de pipeline usam LLM mockado (respostas fixas); qualidade usa eval com score e threshold, nunca assert de string exata.
- **Dependências externas instáveis em teste:** Meta/Evolution/plataformas mockadas com cassettes (VCR) na integração; teste real só nas suítes de contrato marcadas, com números e loja de desenvolvimento.
- **Ambiente de staging divergente de produção:** staging é um projeto Supabase real (não local-only), com as mesmas migrations, PITR e Vault — o drill de restore roda nele.
- **Custo de LLM em evals:** orçamento por eval run monitorado via `llm_calls`; packs base enxutos + variações sob demanda.

---

## 5. Estratégia de testes

### 5.1 Níveis e pirâmide
1. **Unitários (base da pirâmide, rodam em segundos):** lógica pura do runtime — classificação de erros transitório/permanente, cálculo de backoff+jitter, montagem de prompt em camadas, regras de supressão/rate limit, variação de copy (unicidade), parser de payloads por fonte, staleness. LLM e IO sempre mockados.
2. **Testes de banco/funções SQL (pgTAP ou pytest+psycopg contra Postgres real):** `ingest_webhook` (atomicidade, duplicata tripla, unique com conta de origem), contadores de `seq`, CAS estendido (todas as 4 condições, uma a uma), `claim_outbox_batch` (SKIP LOCKED, escopo), funções de segredo (search_path, EXECUTE, validação de tenant), triggers de `expires_at`, índice parcial de versão ativa única.
3. **Suíte de segurança RLS (bloqueante):** para cada tabela de negócio, tentativa de leitura/escrita cross-tenant com JWT de usuário, `worker_role` e `sender_role`; verificação de que roles não têm BYPASSRLS nem ownership; views com `security_invoker`; nenhuma linha vazada = verde.
4. **Integração do pipeline (Postgres + pgmq reais, LLM/APIs mockados):** fluxos 5.1–5.3 da arquitetura ponta a ponta dentro do runtime; todos os cenários de crash (kill em ponto controlado) e redelivery.
5. **Contrato com externos (suítes marcadas, execução manual/semanal):** Cloud API (envio, template, botões, status webhook, `biz_opaque_callback_data`, comportamento real de reenvio — pendência nº 2 da arquitetura), Evolution, OAuth + webhooks de Shopify/Nuvemshop/Yampi (loja dev), API de rastreio.
6. **E2E (staging, Playwright + runtime real):** onboarding completo, gate duplo, hub (inbox realtime, takeover, versões/rollback, estoque, permissões), fluxo de recuperação de ponta a ponta com número de teste.
7. **Carga e resiliência (staging, mensal + pré-BF):** rajada 20–50x por 1h com 25 tenants sintéticos; kills de processo durante a rajada; verificação: zero perda, DLQ vazia ou justificada, pesos respeitados, promoção por idade funcionando, drenagem correta pós-restart.
8. **Evals de IA (harness próprio, por versão de agente):** packs base + variações do tenant; Judge avalia com rubrica; threshold de score para aprovar gate; suíte de red team básica (injeção via mensagem do contato, pedido para revelar prompt, fora de escopo, tentativa de fazer o agente dizer que é IA quando configurado para não dizer).
9. **UAT/Shadow (produção controlada):** os 7 dias de shadow de cada tenant novo SÃO o UAT — 100% avaliado, fila de acompanhamento, ajustes antes da operação plena.

### 5.2 Ambientes e dados
| Ambiente | Uso | Dados |
|---|---|---|
| Local (Supabase CLI + Postgres docker) | unit, SQL, RLS, integração | factories sintéticas; seeds de 3 tenants |
| Staging (projeto Supabase real + VPS de staging) | E2E, carga, resiliência, restore drills, contrato | 25 tenants sintéticos gerados; loja dev por plataforma; números de teste WhatsApp |
| Produção | shadow/UAT por tenant, smoke pós-deploy | dados reais; nenhum teste destrutivo |

Nenhum dado real de conversa é copiado para staging (coerente com ADR-12).

### 5.3 Automação e gates de CI
- **Em todo PR (bloqueante):** unit + SQL/funções + RLS + lint de SQL ad hoc + teste de fronteiras de módulo. Alvo: < 5 min.
- **Em merge para `main` (bloqueante):** + integração do pipeline completa. Alvo: < 15 min.
- **Noturno:** E2E em staging + smoke de contrato mockado.
- **Semanal:** suítes de contrato real (Meta/Evolution/plataformas).
- **Mensal + pré-BF:** carga/resiliência; **trimestral:** os dois drills de restore.
- **Por versão de agente:** eval harness como gate de ativação (nenhuma versão ativa sem score acima do threshold).

### 5.4 Critérios de entrada e saída
- **Entrada por fase:** migrations aplicadas em staging; mocks/cassettes das dependências da fase prontos; dados de fábrica da fase disponíveis.
- **Saída do MVP para o primeiro tenant real:** 100% das suítes P0 verdes (O1–O6, O8); drill de restore executado ao menos 1x com sucesso nos dois caminhos; teste de contrato da Cloud API executado (resolvendo a pendência de duplicidade); eval do primeiro agente acima do threshold; zero defeito aberto de severidade S1/S2.
- **Severidades:** S1 = perda/duplicação de mensagem, vazamento cross-tenant, envio a suprimido (para tudo); S2 = invariante de ordenação/starvation/purga incompleta (bloqueia release); S3 = funcional sem risco de dano (prioriza backlog); S4 = cosmético.

---

## 6. Atividades e estimativas

Premissa: Bruno + Claude Code, testes escritos junto com cada módulo (não em fase separada no fim). Estimativas em dias úteis de trabalho focado; "≈" indica intervalo conforme atrito com APIs externas. O esforço de teste embutido corresponde a ~30–35% do esforço total de desenvolvimento — proporção padrão para sistema com invariantes de concorrência.

| # | Atividade | Entregável | Estimativa |
|---|---|---|---|
| A0 | Infra de teste: harness pytest + factories, Supabase CLI local, staging (projeto + VPS), CI com gates, cassettes base | esqueleto rodando no CI | 3–4 d |
| A1 | Suítes SQL/funções: `ingest_webhook`, contadores seq, CAS estendido (4 condições isoladas), claim functions, segredos, triggers | ~40–50 casos pgTAP/pytest | 4–5 d |
| A2 | Suíte RLS tripla + lint SQL + fronteiras de módulo (bloqueantes) | suíte de segurança no CI | 2–3 d |
| A3 | Unit do runtime: backoff, classificação de erros, prompt em camadas, supressão/rate limits, anti-ban, staleness, parsers | ~80–100 casos | 4–5 d (diluídos no dev de cada módulo) |
| A4 | Integração do pipeline: coalescer (crash claim→send), redelivery, lease/CAS com mensagem injetada na FASE 2, weighted polling + aging, semáforo, DLQ | ~30 cenários, incl. 6 de kill controlado | 5–6 d |
| A5 | Integração outbox/sender: claim batch, lease sending, unknown conservador, correlação status webhook, kill mid-send, revisão manual | ~20 cenários | 3–4 d |
| A6 | Integração dispatch: funis, cancelamento por order_paid, supressão 3 vias, limites, tier Meta, warm-up/teto Evolution | ~25 cenários | 3 d |
| A7 | Contrato real: Cloud API (incl. teste de duplicidade — pendência nº 2), Evolution, OAuth+webhooks das 3 plataformas, rastreio | relatório de contrato + cassettes atualizados | 3–4 d (dependente de aprovações externas) |
| A8 | Eval harness: runner de cenários, rubricas do Judge, thresholds, red team básico, relatório de gate | harness + packs base | 4–5 d |
| A9 | E2E: onboarding completo (Playwright), gate duplo, hub (inbox realtime, takeover, versões, estoque CSV/Sheets, permissões) | ~20 jornadas | 4–5 d |
| A10 | LGPD: purga TTL, purga de lojista, purga por contato (com verificação de embeddings/derivados), opt-out (3 fluxos) | ~12 cenários | 2 d |
| A11 | Carga + resiliência: gerador de rajada 20–50x, 25 tenants sintéticos, kills durante rajada, relatório (perda=0, DLQ, latências) | script reutilizável pré-BF | 3 d |
| A12 | Restore drills: caminho PITR/projeto novo e caminho pg_dump, com Vault e senhas de roles; runbook revisado com tempos reais | runbook validado + RTO medido | 1,5 d por drill (2 drills) |
| A13 | Smoke de produção pós-deploy + checklist de shadow por tenant | checklist automatizado | 1 d |

**Total do esforço de teste: ≈ 38–47 dias úteis**, executados em paralelo às fases de desenvolvimento (não sequenciais). Ordem recomendada de execução: A0 → A1+A2 (junto com o schema) → A3+A4 (junto com o runtime) → A5+A6 → A7 → A8 → A9 → A10 → A11+A12 → A13. O caminho crítico de calendário é A7 (aprovações/contas externas) — iniciar os cadastros (Meta, lojas dev) já na semana 1.

**Métricas acompanhadas durante a execução:** invariantes cobertos / total (alvo 100% P0); tempo de CI; flakiness (< 1% — teste flaky é bug); defeitos por severidade e idade; score médio dos evals por versão; perda e duplicação medidas na carga (alvo: 0 e 0-com-revisão-manual).

---

## 7. Papéis, entrega e manutenção do plano

- **Bruno** é executor e aprovador; o gate duplo com o cliente e o shadow de 7 dias funcionam como aceitação de negócio por tenant.
- Este plano é versionado no repositório junto de `ARCHITECTURE.md`; toda mudança de invariante na arquitetura exige atualizar a suíte correspondente **no mesmo PR** (fitness function de processo).
- Relatório de execução por release: suítes rodadas, resultados, defeitos abertos/fechados, decisão go/no-go registrada.

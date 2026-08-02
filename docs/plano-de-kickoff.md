# Kickoff do desenvolvimento — do repo de specs até produção

## Contexto

O repositório contém a especificação aprovada (em `core/`) de um SaaS B2B multi-tenant: agentes de IA no WhatsApp que recuperam vendas perdidas (carrinho, checkout, PIX) e fazem atendimento completo para lojas Shopify/Nuvemshop/Yampi. Arquitetura aprovada (v1.3): monólito modular — Hub (Next.js/Vercel) + Ingestão (Supabase Edge Functions) + Runtime (Python asyncio único em Docker na VPS), com Postgres/Supabase como fonte de verdade única (dados, filas pgmq, outbox, vetores, evals).

**Não existe código ainda.** O objetivo é iniciar a execução seguindo a Ordem de Execução v2.0 (`core/ordem-de-execucao.md`): dual-track **test-first** (motor) + **design-first** (UI), marcos E0→E8, com o ritmo diário vermelho → verde → refatorar → commit.

**Decisões do Bruno nesta sessão:**
- Este repo (`agents-worder`) vira o monorepo (`runtime/`, `hub/`, `supabase/`, `.github/workflows/` ao lado de `core/`).
- Já existem: projeto Supabase, app Meta/WhatsApp, lojas dev + Evolution. **Faltam: VPS de staging e contas Logfire/Grafana Cloud.**
- O design está pronto no **Claude Design** (projeto `e1663386-2e39-46c6-a6e5-829b532d1362`, lido via DesignSync): `Agents Worder - Design System.dc.html` (**"Obsidian Glass" v1.0** — 13 seções: cor, tipografia, liquid glass 3 níveis, grade/raio/elevação, botões, campos e controles, status/badges/feedback, navegação, dados, conversa, sobreposições, tema light, princípios) + telas `Dashboard`, `Formulário` e `Hub` (usadas nos marcos E4/E5). Fundamentos: dark padrão com paridade light, marca única `#F97316` (só ação/estado/dado), Geist + Geist Mono, breakpoint mobile < 860px, alvo de toque 44px.

## O caminho até produção (visão macro)

| Marco | Entrega | Prova de conclusão |
|---|---|---|
| **E0** Fundação + Design System (6–9d) | Monorepo, harness de testes completo, CI bloqueante, design system integrado, infra staging | CI verde com 1 teste de cada nível; 1 componente passando regressão visual desktop+mobile |
| **E1** Steel thread do motor (7–10d) | webhook → `ingest_webhook` → coalescer → lease/CAS → outbox → sender → WhatsApp real (resposta fixa) | Demo do abandono chegando no WhatsApp; matar o runtime → nada se perde |
| **E2** Agente real (10–14d) | Rubricas de eval ANTES do agente; prompt em camadas, tools, Judge 1, pgvector | Pack de cenários ≥ limite mínimo; custo/latência no Logfire |
| **E3** Recuperação completa (7–10d) | Funis, supressão, rate limits, staleness, anti-ban Evolution | Simulação do dia real + primeira carga leve (10x) |
| **Paralelo E1–E3** | UI estática do formulário (A1–A2.7) com estado local + regressão visual | — |
| **E4** Onboarding self-service (8–10d) | Ligar wizard ao backend: OAuth, Embedded Signup, agente gerador, gate duplo | Onboarding inteiro sem tocar no banco, < 1 dia |
| **E5** Hub operacional (9–12d) | Dashboard, inbox realtime + takeover, versões/rollback, catálogo, funis | 10 jornadas E2E verdes desktop+mobile |
| **E6** Admin + observabilidade (5–7d) | Gate, shadow, alertas, DLQ/outbox, LGPD, dashboards Grafana | Cada falha simulada → alerta certo no canal certo |
| **E7** Endurecimento + piloto (7–10d + 7d shadow) | Carga com critérios quantitativos, restore drills, revisão de segurança, 1º tenant real | Tenant real pós-shadow, zero S1/S2, alertas silenciosos 72h → **"produção"** |
| **E8** Conectores restantes (4–6d) | Nuvemshop e Yampi sobre a porta provada | 2º e 3º tenants reais |

Total estimado: 66–92 dias úteis.

## O que vamos executar agora: E0 — Fundação + Design System

> **Detalhamento executável item a item: [`plano-e0-fundacao.md`](plano-e0-fundacao.md).** O esboço abaixo continua válido como visão geral; o plano do E0 o substitui na hora de executar.

### Fase 1 — Esqueleto do monorepo (Claude)

Estrutura conforme `core/testes-e-cicd.md` §6.4:

```
runtime/           # Python (uv ou poetry), Dockerfile
  tests/{unit,db,pipeline,contract,load}/
hub/               # Next.js (create-next-app), e2e/ (Playwright)
supabase/          # config, migrations/, functions/ (edge)
.github/workflows/ # pr.yml, main.yml (nightly/weekly/load/release entram depois)
```

- `pyproject.toml` com pytest + marcadores `unit`, `db`, `rls`, `pipeline`, `contract`; ruff; relógio injetável desde o dia 1 (`freezegun` nos testes).
- Convenção de idioma: código/identificadores/commits em inglês; copy de UI em PT-BR.
- Preencher a seção **Commands** do `CLAUDE.md` (hoje é placeholder) no mesmo PR em que os comandos nascerem.

### Fase 2 — Harness de testes + CI bloqueante (Claude)

- **1 teste trivial de cada nível** para provar o harness: `unit` (puro), `db`/`rls` (Postgres efêmero via Supabase CLI no CI), `pipeline` (runtime mínimo + pgmq), E2E Playwright (página inicial do hub).
- Gates do PR (`pr.yml`): ruff/eslint + lint anti-SQL fora do repositório de dados + teste de fronteiras de módulo (import-linter) + `unit` + `db` + `rls`, alvo < 5 min.
- `main.yml`: PR + `pipeline` completa (por ora, o teste trivial) → deploy staging (ativado quando a infra existir).
- Migration inicial mínima no `supabase/migrations/` só para provar o ciclo (ex.: tabela `tenants` esqueleto conforme `core/dicionario-de-dados.md`) — o schema completo nasce test-first no E1.

### Fase 3 — Implementar o design system "Obsidian Glass" + regressão visual (Claude)

Fonte: `Agents Worder - Design System.dc.html` do projeto Claude Design (já lido via DesignSync — o design é contrato; divergência é bug).

- **Tokens** extraídos das seções 01–04 e 13 para o `hub/` (CSS variables + config Tailwind, temas dark e light): paleta orange 50–900 (base `#F97316`), neutros dark (`#08090C`/`#0F1014`/`#1A1B20`, textos `#F4F4F5`→`#5F6067`) e light (`#F3F2F0`/`#FFFFFF`/`#E9E7E3`), semânticos (sucesso `#34D399`, atenção `#FACC15`, erro `#F43F5E`), tipografia Geist/Geist Mono com a escala nomeada (display 44 → label mono 10), espaçamento base 2, raios 8/12/18–22/full, os 3 níveis de vidro (chrome blur 28 / card blur 24 / overlay blur 40) e as medidas de layout (sidebar 242px, coluna 340px, leitura 680px, mobile < 860px, toque 44px).
- **Componentes base** conforme as seções 05–11: botões (primary/secondary/ghost/danger, sm/md/lg com estados), campos (input/textarea com foco/erro), toggle, cards de escolha única, chips múltiplos, badges de status (ativo/pausado/em aprovação/shadow/cancelado) e badges técnicos, toasts/alertas, navegação, tabela de dados, balão de conversa, sobreposições (modal/popover/menu). Regra do vidro: nunca empilhar dois níveis (vidro dentro de vidro vira superfície sólida sem blur).
- **Página-vitrine** dos componentes reproduzindo o showcase + **harness de regressão visual** no Playwright: viewports desktop e mobile, linhas de base **só no CI** (nunca captura local), baselines commitadas.
- As telas `Dashboard`, `Formulário` e `Hub` do mesmo projeto ficam como contrato visual para os marcos E4/E5 — não são implementadas no E0.

### Fase 4 — Infra e cadastros externos (Bruno provisiona → Claude configura)

- **Bruno:** contratar VPS de staging; criar contas Logfire + Grafana Cloud; conferir status da verificação Meta/Embedded Signup, números de teste, webhooks das lojas dev e instância Evolution (gap-check do que já existe).
- **Claude:** Docker Compose do runtime na VPS, Grafana Alloy, OpenTelemetry com **span de teste chegando no Logfire E no Grafana Cloud**; secrets no Vault do Supabase acessíveis só pelas funções escopadas (padrão do ADR-11 desde o primeiro secret).

### Prova de conclusão do E0 (Definição de Pronto)

1. CI verde com um teste de cada nível rodando;
2. Um componente do design system renderizado passando regressão visual contra a linha de base, desktop e mobile;
3. Span de teste visível no Logfire e no Grafana Cloud;
4. Gates bloqueantes ativos antes da primeira feature.

## Depois do E0 → E1 (próximo plano)

O E1 começa escrevendo **primeiro** (vermelhos) as suítes A1/A2 de DB e os cenários 1–10 da pipeline (`core/testes-e-cicd.md` §3) — eles são a especificação executável do motor: `ingest_webhook` atômica, contadores de `seq`, CAS estendido, coalescer com generation, outbox com lease. A implementação existe para apagá-los um a um. Será planejado em detalhe ao fim do E0.

## Pendências a resolver com o Bruno (não bloqueiam o E0)

1. **`core/formulario-perguntas.md`** é referenciado no CLAUDE.md mas **não existe** no repo — necessário antes do E4 (gerador de prompt mapeia só desses campos).
2. **Escolha do LLM do agente** (pendência nº 5 da arquitetura) — necessária no início do E2.
3. Teste de duplicidade da Cloud API (pendência nº 2) — entra na suíte `contract` do E1/E7.

## Verificação

- Cada fase termina com o CI verde no GitHub Actions (não só local).
- `pytest -m unit`, `-m db`, `-m rls`, `-m pipeline` e `pnpm exec playwright test` rodam documentados na seção Commands do CLAUDE.md.
- Regressão visual: PR de teste alterando um componente deve **falhar** o CI (prova de que o harness pega divergência).
- Observabilidade: span de teste com `trace_id` visível nos dois destinos.

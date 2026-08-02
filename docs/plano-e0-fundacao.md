# Plano E0 — Fundação + Design System

**Versão:** 1.0 · **Data:** 2026-08-01 · **Base:** `core/ordem-de-execucao.md` v2.0 (marco E0) · `core/testes-e-cicd.md` v1.1 · `core/arquitetura-plataforma-agentes-whatsapp.md` v1.3 · `core/observabilidade-e-monitoramento.md` v2.0 · `docs/plano-de-kickoff.md`
**Escopo:** detalhamento executável do primeiro marco. Estimativa da ordem de execução: **6–9 dias úteis**.

---

## 1. O que o E0 entrega (e o que não entrega)

O E0 não entrega nenhuma funcionalidade do produto. Ele entrega a **capacidade de construir o produto com as duas orientações do projeto ativas**: nada de código sem teste que o exija (R1), nada de tela sem design que a defina (R2). Depois do E0, toda entrega do E1 em diante nasce dentro desses trilhos.

**Entra:**
- monorepo com toolchain de runtime, hub, banco e CI;
- um teste real de cada nível (`unit`, `db`, `rls`, `pipeline`, E2E) provando que o harness funciona;
- gates bloqueantes de PR ativos **antes da primeira feature**;
- design system "Obsidian Glass" implementado (tokens + componentes) com página-vitrine e regressão visual nos dois viewports;
- observabilidade instrumentada desde o primeiro span, com o span de teste chegando nos dois backends;
- o padrão de segredo do ADR-11 aplicado ao primeiro segredo que existir.

**Não entra** (para evitar deriva): telas Dashboard/Formulário/Hub; schema completo do dicionário de dados; `ingest_webhook` e qualquer motor (é E1); workflows `nightly`/`weekly`/`load`/`release`; `core/formulario-perguntas.md`.

---

## 2. Decisões desta sessão (fixadas)

| Decisão | Escolha | Consequência |
|---|---|---|
| Escopo do marco | E0 completo, com a trilha de infra **paralela** | T1–T3 nunca ficam bloqueadas esperando VPS ou contas externas |
| Contrato visual | **Claude Design** (`Agents Worder - Design System.dc.html`, Obsidian Glass v1.0) como fonte única | `CLAUDE.md` e `core/ordem-de-execucao.md` §R2 ainda citam Figma — corrigir por PR de documentação (item E0-05) |
| Linha de base visual | **Gerada no CI** e aprovada por revisão de PR | Captura local nunca vira baseline; fidelidade ao design é conferida na revisão, estabilidade é garantida pelo ambiente fixo |
| Toolchain Python | **uv** (`uv.lock` versionado) | CI mais rápido; `uv` também fixa a versão do Python |

---

## 3. Como o test-first se aplica a uma fase de bootstrap

E0 é majoritariamente andaime: quase não há regra de negócio para especificar antes. A regra que impede R1 de virar teatro:

> **Todo item do E0 entrega junto a sua prova executável, e a prova roda vermelha antes de o item existir.**

E, principalmente: **um gate que nunca reprovou não é um gate.** As quatro provas negativas abaixo são parte da definição de pronto do marco, não um extra.

| # | Sabotagem deliberada | Gate que precisa reprovar |
|---|---|---|
| N1 | `channels` importa `connectors` | teste de fronteiras de módulo (import-linter) |
| N2 | `SELECT` escrito fora da camada de repositório | teste-fitness anti-SQL (nível `unit`) |
| N3 | padding de um botão alterado | regressão visual (desktop **e** mobile) |
| N4 | leitura cross-tenant com `worker_role` | suíte `rls` |

Cada sabotagem é feita em PR descartável, o run vermelho é registrado neste documento (link) e o PR é fechado sem merge.

---

## 4. Trilhas e dependências

```
T1 Esqueleto do monorepo ──┬──▶ T2 Harness de testes + CI bloqueante
   (E0-01 … E0-05)         │      (E0-06 … E0-12)
                           └──▶ T3 Design system Obsidian Glass
                                  (E0-13 … E0-18)   [depende do Playwright de E0-09]

T4 Infra + observabilidade (E0-19 … E0-23)  ──── paralela, com pré-requisitos do Bruno
```

- **T2 depende de T1** inteiro.
- **T3 depende de T1** (scaffold do hub) e do **E0-09** (Playwright configurado).
- **T4 é paralela**: só o E0-23 (ligar deploy de staging) depende de T2, e todos os itens dependem dos pré-requisitos do Bruno (§9).
- Ordem recomendada de PRs: `E0-01…05` → `E0-06…09` → `E0-10…12` → `E0-13…18` (T4 intercalada assim que a infra existir).

---

## 5. Trilha T1 — Esqueleto do monorepo

### E0-01 · Layout do repositório
**Objetivo:** criar `runtime/`, `hub/`, `supabase/`, `.github/workflows/` ao lado de `core/` e `docs/`; `.gitignore`, `.editorconfig`, `README.md` curto apontando para `core/` como especificação.
**Prova:** este item não tem prova própria — ele é validado pelo primeiro CI verde (E0-10). Se ao fim de T2 algum caminho da estrutura não é exercido por nenhum job, ele não deveria existir.
**Pronto quando:** a estrutura de `core/testes-e-cicd.md` §6.4 existe integralmente, sem pastas órfãs.

### E0-02 · `runtime/` — uv, pytest, ruff, módulos
**Objetivo:**
- `pyproject.toml` com `[tool.pytest.ini_options]` declarando os marcadores `unit`, `db`, `rls`, `pipeline`, `contract` e `--strict-markers` (marcador não declarado é erro, não aviso);
- `uv.lock` versionado; `.python-version` fixando a versão;
- ruff configurado, incluindo a regra que **proíbe `print`** (obrigatório: logging estruturado é via SDK do Logfire);
- os 11 módulos da arquitetura §3 nascem como pacotes vazios — `channels`, `connectors`, `agent_core`, `tools`, `judges`, `dispatch`, `queueing`, `inbox`, `onboarding`, `quota`, `obs` — mais `repository` (única camada autorizada a conter SQL);
- `Dockerfile` multi-stage (build com uv → imagem slim), usuário não-root;
- `tests/{unit,db,pipeline,contract,load}/`.

**Prova:** `uv run pytest -m unit` executa (falha por ausência de teste até o E0-06); `uv run ruff check .` verde; `docker build` verde.
**Pronto quando:** os três comandos acima rodam localmente e estão citados na seção Commands do `CLAUDE.md`.

### E0-03 · `hub/` — Next.js, pnpm, Playwright
**Objetivo:** `create-next-app` (TypeScript, App Router), eslint, pnpm com lockfile; Playwright configurado com **dois projects fixos** — `desktop` 1440×900 e `mobile` 390×844 — e as fontes **Geist e Geist Mono carregadas localmente** via `next/font/local` (nunca CDN: fonte de rede é a causa nº 1 de baseline visual instável).
**Prova:** `pnpm build` verde; `pnpm exec playwright test` executa a suíte vazia nos dois projects.
**Pronto quando:** os dois viewports aparecem no relatório do Playwright.

### E0-04 · `supabase/` — config, migrations, functions
**Objetivo:** `supabase/config.toml` com as extensões que o projeto exige (**pgmq**, **pgvector**, Vault); `migrations/` e `functions/` vazias.
**Prova:** `supabase start` sobe local e `supabase db reset` aplica migrations sem erro.
**Risco a validar aqui, cedo:** confirmar que a imagem local do Supabase CLI traz `pgmq` e `pgvector` na versão usada. Se não trouxer, o plano B é um serviço Postgres próprio no CI com as extensões instaladas — decidir **neste item**, não no E0-08.

### E0-05 · `CLAUDE.md`, convenções e correção de documentação
**Objetivo:**
- preencher a seção **Commands** do `CLAUDE.md` (hoje placeholder) com os comandos que passaram a existir;
- registrar a convenção de idioma (docs e copy em PT-BR; código, identificadores, comentários e commits em inglês);
- **corrigir as referências a Figma** em `CLAUDE.md` e em `core/ordem-de-execucao.md` §R2 para Claude Design, conforme a decisão do §2.

**Prova:** todo comando citado na seção Commands é executado por algum job do CI (se o CI não roda, o comando apodrece).
**Pronto quando:** não resta menção a Figma como fonte de verdade visual.

---

## 6. Trilha T2 — Harness de testes + CI bloqueante

### E0-06 · Relógio injetável + primeiro teste `unit`
**Objetivo:** `Clock` como protocolo (`now()`, `sleep()`), `SystemClock` para produção e relógio congelado nos testes. Junto, o **teste-fitness que proíbe relógio direto**: falha se `datetime.now()`, `time.time()` ou `asyncio.sleep` aparecerem fora da camada de adaptadores.
**Por que é o primeiro:** debounce (10s), staleness, cooldowns (72h), warm-up e TTL (12 meses) são testados sem espera real — se o relógio não for injetável desde o primeiro dia, o E3 fica intestável.
**Prova (vermelho→verde):** o teste do `Clock` é escrito antes do módulo existir (falha de import); depois, a sabotagem de introduzir um `datetime.now()` em `dispatch` precisa reprovar.
**Pronto quando:** `uv run pytest -m unit` verde com os dois testes.

### E0-07 · Migration inicial, roles e a suíte `rls` com dentes
**Objetivo:** migration `0001` com o mínimo que dá **poder de asserção** à suíte de segurança:
- `tenants` conforme `core/dicionario-de-dados.md` §1.1 (status com CHECK, `retention_months` 12–24, `never_say_ai` default true, etc.);
- `profiles` §1.2 e `memberships` §1.3 — `memberships` é a primeira tabela com `tenant_id`, ou seja, o primeiro alvo real de RLS;
- roles `worker_role` e `sender_role` (sem `BYPASSRLS`, não donos das tabelas), pools separados previstos na config;
- políticas RLS nos três caminhos: JWT do usuário (via `memberships`), `worker_role` e `sender_role` com `SET LOCAL app.tenant_id`.

**Justificativa da ampliação:** o plano de kickoff previa "migration mínima só para provar o ciclo". Só `tenants` não permite testar vazamento cross-tenant, e a suíte `rls` é **bloqueante desde o primeiro PR** (`core/testes-e-cicd.md` §3.2). O custo extra no E0 compra o gate mais importante do projeto nascendo com dentes.

**Prova (vermelho→verde):** escrever primeiro o teste `rls` que tenta ler `memberships` do tenant B com as três credenciais do tenant A — ele **passa a vazar** (vermelho) enquanto não houver policy, e fecha quando a policy existir. Somar os testes de que os roles não têm `BYPASSRLS` e não são donos das tabelas.
**Pronto quando:** `pytest -m db` e `pytest -m rls` verdes; sabotagem N4 reprova.

### E0-08 · Primeiro teste `pipeline` com pgmq real
**Objetivo:** provar o nível mais caro do harness com o mínimo: `pgmq.send` → `read(vt)` → `archive` contra o Postgres efêmero, mais o esqueleto de laço do runtime (subir, consumir um tick, desligar graciosamente).
**Cuidado:** este item **não** implementa o `queueing` de verdade (weighted polling, backoff, semáforo) — isso é E1, e nasce dos seus próprios testes vermelhos. Aqui só se prova que o nível `pipeline` roda no CI.
**Prova:** teste escrito antes do laço existir.
**Pronto quando:** `pytest -m pipeline` verde e o processo encerra sem deixar mensagem em limbo.

### E0-09 · Primeira jornada E2E
**Objetivo:** jornada Playwright que abre a home do hub e afirma um marcador estável, rodando nos dois projects.
**Prova:** escrita antes de a home existir.
**Pronto quando:** verde em `desktop` e `mobile`.

### E0-10 · `pr.yml` — o gate bloqueante
**Objetivo:** um workflow com alvo de **< 5 min**, com cache de `uv` e `pnpm` e `concurrency: cancel-in-progress`:

| Job | Conteúdo |
|---|---|
| `lint` | ruff (inclui a proibição de `print`) + eslint |
| `boundaries` | import-linter com os contratos da arquitetura §3 — no mínimo: `channels` ⊘ `connectors`; `agent_core` e `dispatch` ⊘ `channels` (nada chama a API do WhatsApp exceto senders); todo módulo ⊘ SQL direto |
| `tests-py` | `pytest -m unit` + `-m db` + `-m rls` com Postgres efêmero |

**Alteração registrada (E0-06):** o job `sql-lint` previsto aqui **deixou de existir como job**. A trava "SQL fora de `repository/`" foi implementada como teste-fitness de nível `unit` (`runtime/tests/unit/test_no_sql_outside_repository.py`), pelo mesmo motivo da trava do relógio: roda no gate `unit`, é reproduzível localmente sem CI e detecta por AST em vez de regex (uma docstring que cita `SELECT` não é violação; uma query montada em string é). O gate continua bloqueante — só mudou de job para asserção. A sabotagem N2 passa a ser verificada contra `tests-py`.

**Prova:** a própria sabotagem N1 e N2.
**Pronto quando:** os quatro jobs são checks obrigatórios na proteção de branch da `main`.

### E0-11 · `main.yml`
**Objetivo:** reusar o `pr.yml` e somar `pytest -m pipeline`; o passo de deploy em staging já existe, **atrás de flag/condicional**, desligado até o E0-23.
**Pronto quando:** merge na `main` roda tudo em < 15 min.

### E0-12 · Executar e registrar as quatro provas negativas
**Objetivo:** rodar N1–N4 em PRs descartáveis e registrar aqui os links dos runs vermelhos.
**Pronto quando:** as quatro reprovaram pelo motivo certo (não por erro colateral) e os PRs foram fechados sem merge.

| Prova | PR | Run vermelho | Job que reprovou | Data |
|---|---|---|---|---|
| N1 fronteiras | [#2](https://github.com/matheusmarques6/agents-worder/pull/2) (fechado) | [30735983063](https://github.com/matheusmarques6/agents-worder/actions/runs/30735983063) | `boundaries` — `agents_runtime.channels -> agents_runtime.connectors (l.10)` | 2026-08-02 |
| N2 SQL solto | [#3](https://github.com/matheusmarques6/agents-worder/pull/3) (fechado) | [30735947653](https://github.com/matheusmarques6/agents-worder/actions/runs/30735947653) | `tests-py` — `test_module_contains_no_sql[dispatch/__init__.py]`, 1 de 89 | 2026-08-02 |
| N3 regressão visual | — | — | pendente do E0-17/E0-18 (trilha T3) | — |
| N4 vazamento RLS | [#4](https://github.com/matheusmarques6/agents-worder/pull/4) (fechado) | [30735956514](https://github.com/matheusmarques6/agents-worder/actions/runs/30735956514) | `tests-py` — 11 falhas da suíte `rls`, todas leitura/escrita cross-tenant | 2026-08-02 |

**Cada uma reprovou sozinha.** Nos três PRs, os outros três jobs passaram — é isso que separa "a trava pegou" de "alguma coisa quebrou". A primeira tentativa da N1 reprovou também no `lint` (um `# noqa: E402` desnecessário virou `RUF100`); a sabotagem foi corrigida e repetida até o vermelho ser só o do `boundaries`.

**Divergência deliberada na N4.** O plano dizia "remover a policy `memberships_worker_scoped`". Remover a policy **fecha** a tabela — o worker deixa de ver qualquer linha — e a suíte reprovaria nas asserções positivas, por privilégio ausente. Privilégio ausente não é prova de RLS. A sabotagem executada foi `alter table public.memberships disable row level security`, que é o que produz leitura cross-tenant de verdade: as 11 falhas são todas do tipo `assert [(UUID(...),)] == []`.

---

## 7. Trilha T3 — Design system "Obsidian Glass"

Fonte única: `Agents Worder - Design System.dc.html` (projeto Claude Design `e1663386-2e39-46c6-a6e5-829b532d1362`), 13 seções. O design é contrato: divergência é bug ou mudança explícita de escopo.

### E0-13 · Tokens (seções 01–04 e 13)
**Objetivo:** extrair para o `hub/` como CSS variables + configuração do Tailwind, com **dark padrão e paridade light**:
- marca laranja 50–900 (base `#F97316`), usada **só em ação, estado e dado** — nunca como decoração;
- neutros dark (`#08090C` / `#0F1014` / `#1A1B20`; textos `#F4F4F5` → `#5F6067`) e light (`#F3F2F0` / `#FFFFFF` / `#E9E7E3`);
- semânticos: sucesso `#34D399`, atenção `#FACC15`, erro `#F43F5E`;
- tipografia Geist / Geist Mono com a escala nomeada (display 44 → label mono 10);
- espaçamento base 2; raios 8 / 12 / 18–22 / full;
- vidro em 3 níveis: chrome blur 28, card blur 24, overlay blur 40;
- medidas de layout: sidebar 242px, coluna 340px, largura de leitura 680px, **breakpoint mobile < 860px**, alvo de toque 44px.

**Prova:** teste de contrato dos tokens — uma lista esperada de nomes de token que falha se algum sumir ou for renomeado. Impede que um token seja "esquecido" e substituído por valor solto.
**Pronto quando:** nenhum valor hexadecimal literal existe fora do arquivo de tokens (regra de lint).

### E0-14 · Primitivo de vidro e a regra de não empilhamento
**Objetivo:** um primitivo `Glass` com os três níveis. A regra do design — **nunca empilhar dois níveis de vidro; vidro dentro de vidro vira superfície sólida sem blur** — é implementada por contexto, não por disciplina do desenvolvedor.
**Prova:** teste de componente que aninha dois `Glass` e afirma que o interno perdeu o blur. É a regra do design virando asserção.

### E0-15 · Componentes (seções 05–11), em quatro lotes
| Lote | Componentes |
|---|---|
| L1 · ação e entrada | botões primary/secondary/ghost/danger em sm/md/lg com todos os estados (incluindo **lg 48 no mobile**); input; textarea; foco e erro; toggle; card de escolha única; chips múltiplos |
| L2 · status e feedback | badges de status (ativo, pausado, em aprovação, shadow, cancelado); badges técnicos; toast; alerta; skeleton; estado vazio |
| L3 · navegação e dados | sidebar agrupada (242px); topbar; **tab bar de vidro do mobile (4 destinos)**; tabela de dados; paginação |
| L4 · conversa e sobreposição | balão de conversa (entrada/saída/status); composer; modal; popover; menu |

**Prova por lote:** testes de componente das variantes e estados + entrada na vitrine + baseline visual gerada no CI.
**Pronto quando:** todo componente do showcase do design existe e nenhuma tela futura precisará de CSS avulso.

### E0-16 · Página-vitrine
**Objetivo:** rota `/_design` **dentro do `hub/`** (sem pacote separado — não há segundo consumidor que justifique a extração), reproduzindo o showcase do design com todos os estados. Fora do build de produção por flag de ambiente.

### E0-17 · Harness de regressão visual
**Objetivo:** comparação por `toHaveScreenshot` nos dois projects, com as decisões que tornam o resultado estável:
- baselines **geradas exclusivamente no CI** e commitadas; execução local com baseline ausente **falha**, nunca gera;
- animações desabilitadas e `prefers-reduced-motion` na captura;
- fontes locais (E0-03);
- `maxDiffPixelRatio` pequeno, porém não zero — `backdrop-filter` não é determinístico ao nível do pixel;
- vitrine renderizada sobre fundo estático.

**Decisão registrada — mobile vem de regra, não de frame:** o design system define breakpoint, alvo de toque, tamanho de botão mobile e tab bar, mas **nenhuma tela do projeto tem frame mobile** (§10, pendência 1). No E0 isso não é um problema: os componentes são implementáveis nos dois viewports a partir das regras. A baseline mobile é, portanto, **gerada da implementação e aprovada na revisão do PR** — ela protege contra regressão, não certifica fidelidade a um desenho que não existe.

### E0-18 · Prova negativa visual (N3)
**Objetivo:** alterar o padding de um botão em PR descartável e confirmar que a regressão visual reprova **nos dois viewports**.

---

## 8. Trilha T4 — Infra e observabilidade (paralela)

### E0-19 · Docker Compose + Alloy
**Objetivo:** compose com runtime + **Grafana Alloy** como sidecar; `alloy/config.alloy` versionado com receiver OTLP (`0.0.0.0:4318`) → `memory_limiter` → `batch` → exporter otlphttp para o gateway da Grafana Cloud, com credenciais só via `sys.env()`. Incluir já o processor de **redação de PII** (atributos de conteúdo e telefone descartados) — é a segunda linha de defesa exigida pela observabilidade §2.2.

### E0-20 · Módulo `obs/` do runtime
**Objetivo:** `logfire.configure(service_name="agents-runtime", environment=DEPLOY_ENV)` carregado antes de tudo no `main.py`, com `service.version` = SHA do deploy; `instrument_httpx`, `instrument_psycopg`, `instrument_system_metrics`; cópia OTLP para o Alloy local por variável de ambiente.
**Prova:** teste unitário que falha se `service.name` ou `deployment.environment` estiverem ausentes nos resource attributes — sem eles tudo vira `unknown_service` nos dois backends.

### E0-21 · Span de teste nos dois backends
**Objetivo:** comando que emite um span de teste e imprime o `trace_id`.
**Prova de conclusão:** o **mesmo `trace_id`** localizado no Logfire **e** no Grafana Cloud (Tempo). É uma das quatro provas do marco.

### E0-22 · Primeiro segredo pelo padrão ADR-11
**Objetivo:** ao surgir o primeiro segredo, ele já entra no Vault do Supabase com o padrão definitivo: funções `SECURITY DEFINER` escopadas (`get_channel_secret`, `get_connector_secret`) com `search_path` fixo, nomes totalmente qualificados, `EXECUTE` revogado de `PUBLIC` e concedido só ao role correspondente. **Nenhum GRANT em `vault.decrypted_secrets`.**
**Prova (`-m db`, conforme testes §3.1.6):** `get_channel_secret` executável por `sender_role` e **negada** a `worker_role`, e o inverso para a função de conector; tenant ou conta inválidos → erro.
**Nota de escopo:** só as funções e os testes. Consumidores reais são E1.

### E0-23 · Ligar o deploy de staging
**Objetivo:** habilitar no `main.yml` a sequência obrigatória de `core/testes-e-cicd.md` §6.3 — migrations → edge functions → runtime (desligamento gracioso) — e a fumaça mínima.
**Depende de:** VPS provisionada (§9).

---

## 9. Pré-requisitos do Bruno (trilha T4)

Nada aqui bloqueia T1–T3. Quanto antes existirem, mais cedo T4 começa — e a verificação da Meta é o item de maior risco de calendário do projeto inteiro (`ordem-de-execucao.md` §5.1).

| # | Item | Necessário para |
|---|---|---|
| B-1 | Contratar VPS de staging | E0-19, E0-23 |
| B-2 | Criar conta Logfire e emitir write token | E0-20, E0-21 |
| B-3 | Criar conta Grafana Cloud (endpoint OTLP, instance ID, token) e habilitar IRM | E0-19, E0-21 |
| B-4 | Gap-check do que já existe: status da verificação Meta **incluindo Embedded Signup**, números de teste, webhooks das lojas dev nas 3 plataformas, instância Evolution | E1 em diante — mas o **disparo** é no E0 |
| B-5 | **Definir o ambiente Supabase de staging.** Existe **um único** projeto (`agents-worder`), sem migrations. A sequência de deploy (`testes-e-cicd.md` §6.3) aplica migrations em staging antes de produção, e o exercício trimestral de restauração exige um projeto descartável. Decidir entre: (a) **segundo projeto Supabase** dedicado a staging — isolamento real, sem risco de E2E tocar produção; (b) **branches do Supabase** — mais barato e efêmero por PR, mas compartilha o projeto e não serve para o drill de restauração. Recomendação: **(a)**, com branches como conveniência opcional por PR | E0-23 e todo o E2E |

---

## 10. Riscos do marco

| # | Risco | Mitigação |
|---|---|---|
| R1 | **`backdrop-filter` instável na regressão visual.** Um design system inteiro sobre três níveis de vidro é justamente o que mais varia entre renderizações | Fontes locais, animações desligadas, fundo estático, `maxDiffPixelRatio` pequeno e não-zero, baseline só do CI. Se ainda oscilar: capturar componentes de vidro sobre plano de fundo sólido de teste — **não** afrouxar o limite até a regressão parar de significar algo |
| R2 | `pgmq`/`pgvector` ausentes na imagem local do Supabase CLI | **Parcialmente resolvido (2026-08-01, verificado via MCP):** o projeto hospedado `agents-worder` (`jmzsxwtflxsrdfjkuusi`, sa-east-1, Postgres 17.6.1) tem `pgmq` 1.5.1 e `vector` 0.8.2 **disponíveis** e o `supabase_vault` 0.3.1 **já instalado**; nenhuma migration aplicada ainda. Resta confirmar as mesmas extensões na imagem local do CLI — validar no E0-04, não no E0-08; plano B é serviço Postgres próprio no CI com as extensões |
| R3 | VPS e contas atrasarem | T4 isolada; deploy de staging atrás de flag desde o E0-11 |
| R4 | Gate de PR passar de 5 min e virar atrito | Cache de `uv`/`pnpm`, jobs paralelos, `cancel-in-progress`; se estourar, o que sai do PR é `pipeline` (já está na `main`), nunca `rls` |
| R5 | Divergência entre design e documentação crescer em silêncio | Os achados do §11 viram PR de documentação ainda no E0; daí em diante, mudança de design e atualização do doc de telas no **mesmo PR** |

---

## 11. Pendências registradas (não bloqueiam o E0)

1. **Não existe layout mobile de nenhuma tela.** Todos os frames de `Agents Worder - Formulário.dc.html` e `Agents Worder - Hub.dc.html` são 1440×900; não há `@media` nem frame de celular. Isso conflita com a premissa 3 de `core/telas-da-aplicacao.md` ("desktop e mobile com o mesmo capricho desde o MVP") e com a regra R2 da ordem de execução. **Decidir antes do E4**, entre: (a) derivar mobile das regras do design system e aceitar baseline auto-referente; (b) produzir frames mobile das ~29 telas; (c) produzir frames mobile só das três telas que o próprio documento marca como críticas no celular — wizard, inbox e dashboard. Recomendação: **(c)**.
2. **Divergências entre o design e `core/telas-da-aplicacao.md`:** telas fundidas no design (A4b+A4c; A6+A7+A8; B5a+B5b; B8b+B8c; B10a+B10b+B10c) e uma etapa nova no wizard, **"Sobre a loja"**, que não existe no documento. Atualizar o documento de telas para refletir o design (o design é o contrato) antes do E4.
3. **Contagem inconsistente:** o documento de telas diz "18 no hub" mas lista 19 (B1–B10c). Corrigir junto com o item 2.
4. **`core/formulario-perguntas.md` é citado no `CLAUDE.md` mas não existe.** Necessário antes do E4 — o gerador de prompt só pode mapear campos rastreáveis a esse documento.
5. **LLM do agente** (pendência 5 da arquitetura) — necessária no início do E2.
6. **Teste de duplicidade da Cloud API** (pendência 2 da arquitetura) — entra na suíte `contract` no E1/E7.

---

## 12. Definição de pronto do E0

O marco está concluído quando **as oito provas** abaixo estiverem verdes ou registradas:

**Positivas**
1. CI verde com um teste real de cada nível rodando: `unit`, `db`, `rls`, `pipeline` e E2E.
2. Um componente do design system passando na regressão visual contra a linha de base, em **desktop e mobile**.
3. Span de teste com o **mesmo `trace_id`** visível no Logfire e no Grafana Cloud.
4. Gates bloqueantes ativos como checks obrigatórios **antes da primeira feature**.

**Negativas** (registradas na tabela do E0-12)

5. N1 — quebra de fronteira de módulo reprova.
6. N2 — SQL fora da camada de repositório reprova.
7. N3 — alteração de um componente reprova a regressão visual nos dois viewports.
8. N4 — tentativa de leitura cross-tenant reprova a suíte `rls`.

Cumprido isso, o E1 começa escrevendo **primeiro** as suítes A1/A2 de DB e os cenários 1–10 de pipeline (`core/testes-e-cicd.md` §3) — a especificação executável do motor, vermelha antes de existir implementação.

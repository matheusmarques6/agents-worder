# Estado da execução — retomada

**Atualizado:** 2026-08-02 · **Marco:** E0 (`docs/plano-e0-fundacao.md`) · **Branch:** `e0-foundation`

Este arquivo é o ponto de retomada entre sessões. Quem chegar aqui lê isto, o plano do E0 e o `CLAUDE.md` — nessa ordem — e continua.

---

## Onde paramos

**Trilha T1 (esqueleto do monorepo) — concluída e commitada** em `eacddb3`.

| Item | Estado | Prova executada |
|---|---|---|
| E0-01 estrutura do repositório | ✅ | `runtime/`, `hub/`, `supabase/` criados |
| E0-02 runtime (uv, pytest, ruff, módulos) | ✅ | `ruff check` verde · `lint-imports` 3/3 contratos · `pytest -m unit` sai 5 (vermelho esperado) |
| E0-03 hub (Next.js, pnpm, Playwright) | ✅ | `pnpm build`, `lint` e `typecheck` verdes (exit 0) |
| E0-04 supabase (config) | ✅ | `config.toml` gerado e ajustado; com o Docker resolvido, `supabase start` (forma enxuta) rodou de fato nos E0-07 e E0-08 e a **R2 foi fechada** — a imagem local traz `pgmq` 1.5.1 e `vector` 0.8.2 |
| E0-05 CLAUDE.md + convenções | ✅ | seção Commands preenchida; duplicação do arquivo removida; Figma → Claude Design |

**Trilha T2 (harness + CI) — iniciada.**

| Item | Estado | Prova executada |
|---|---|---|
| E0-06 relógio injetável + fitness | ✅ commit `8771f6f` | `pytest -m unit` 46 verdes · cada trava vista **vermelha** primeiro, contra sabotagem plantada em `dispatch` (relógio) e em `inbox` (SQL) · `ruff check` e `lint-imports` verdes |
| E0-09 primeira jornada E2E | ✅ commit `c06d122` | `pnpm e2e` 6 verdes (3 asserções × 2 projects) · vista **vermelha** primeiro nos dois viewports · `lint`, `typecheck` e `build` do hub exit 0 |
| E0-07 migration 0001 + suíte `rls` | ✅ commit `cc6406a` | `pytest -m "unit or db"` 70 verdes · vazamento visto **vermelho** com as três credenciais · N4 confirmada no gate final (desligar RLS só em `memberships` reprova 10 dos 24) · **R2 fechado**: imagem local traz `pgmq` 1.5.1 e `vector` 0.8.2 |
| E0-10 `pr.yml` | ✅ | 4 jobs verdes no PR [#1](https://github.com/matheusmarques6/agents-worder/pull/1) em **2m31s** (`boundaries` 11s · `lint` 27s · `tests-py` 1m51s · `tests-hub` 2m27s) — alvo era < 5 min · checks obrigatórios ativos por ruleset, e o push direto na `main` foi visto sendo **recusado** (`4 of 4 required status checks are expected`) |
| E0-11 `main.yml` | ✅ | run [30735758279](https://github.com/matheusmarques6/agents-worder/actions/runs/30735758279): o gate inteiro reusado + `tests-pipeline` 1m33s contra pgmq real; `deploy-staging` pulado pela flag desligada. ~4 min, alvo era < 15 |
| E0-12 provas negativas | ✅ N1/N2/N4 · N3 pendente da T3 | tabela com PR, run e job em `docs/plano-e0-fundacao.md` §E0-12 |
| E0-08 pgmq real + esqueleto do laço | ✅ | `pytest -m pipeline` 9 verdes · cada arquivo visto **vermelho** primeiro (módulo inexistente) · `-m "unit or db"` 89 verdes · a suíte de exposição da fila provada nos dois sentidos por sabotagem (ver decisão 20) · `ruff check` e `lint-imports` verdes |

**Trilha T3 (design system) — iniciada.** Ordem de execução resequenciada: 13 → 14 → 16/17 → 15 → 18, porque a prova de cada lote de componentes é a baseline visual, que exige vitrine e harness de pé.

| Item | Estado | Prova executada |
|---|---|---|
| E0-13 tokens | ✅ PR [#6](https://github.com/matheusmarques6/agents-worder/pull/6) | contrato de tokens visto **vermelho** primeiro (14 falhas nos 2 viewports) · trava de cor vista reprovando contra `#F97316` plantado na home · gate de CI verde |
| E0-16 shell da vitrine | ✅ | 5 asserções × 2 viewports vistas **vermelhas** primeiro · as 13 seções do design na ordem, com testid estável · o interruptor de tema afirmado pela superfície computada do body (#08090C ↔ #F3F2F0), não pelo atributo |
| E0-17 harness de regressão visual | ✅ PRs [#9](https://github.com/matheusmarques6/agents-worder/pull/9) e [#10](https://github.com/matheusmarques6/agents-worder/pull/10) | ciclo inteiro executado **no CI**: gate vermelho por baseline ausente (4 PNGs, `A snapshot doesn't exist`) → `update-baselines` no runner ([30753971421](https://github.com/matheusmarques6/agents-worder/actions/runs/30753971421)) → artefato commitado → gate verde. Nenhuma baseline saiu desta máquina |
| E0-15 · L1 ação e entrada | ✅ PR [#11](https://github.com/matheusmarques6/agents-worder/pull/11) | 12 asserções objetivas vistas **vermelhas** primeiro (altura e raio por tamanho, lg ≥ 44, disabled/loading reais, erro anunciado, foco visível, switch, escolha exclusiva, chips acumulando) · 8 baselines novas geradas no runner · as 4 do vidro **inalteradas**, o que prova que o lote não vazou para o 03 |
| E0-15 · L2 status e feedback | ✅ PR [#12](https://github.com/matheusmarques6/agents-worder/pull/12) | 13 asserções vistas **vermelhas** primeiro · nenhum par de status compartilha cor e todo status escreve o estado por extenso · duas composições entre lotes (toast = `<Glass level="overlay">`, estado vazio consome o Button do L1) · a revisão da baseline light pegou ilegibilidade generalizada, corrigida antes do merge |
| E0-15 · L4 conversa e sobreposição | ✅ PR [#13](https://github.com/matheusmarques6/agents-worder/pull/13) | 13 asserções vermelhas primeiro · **as duas metades da regra do vidro lado a lado no mesmo card**: modal e menu mantêm `blur(40px)`, vidro aninhado perde · modal é `<dialog>` nativo, modalidade provada pelo `::backdrop` · terceira ilegibilidade de light pega pela revisão de imagem |
| E0-15 · L3 navegação e dados | ✅ PR [#14](https://github.com/matheusmarques6/agents-worder/pull/14) | 14 asserções vermelhas primeiro · **o breakpoint virou asserção**: desktop tem sidebar e não tem tab bar, mobile é o inverso exato · pendência do enum fechada (`pending_approval` → `onboarding`) |
| E0-18 · prova N3 | ✅ PR [#15](https://github.com/matheusmarques6/agents-worder/pull/15) (fechado) | run [30760216787](https://github.com/matheusmarques6/agents-worder/actions/runs/30760216787): 4 falhas de `toHaveScreenshot` na seção 05, **nos dois viewports e nos dois temas**, e só o `tests-hub` reprovou · **fecha a tabela das quatro provas negativas e a T3** |
| E0-14 primitivo Glass | ✅ | 8 asserções de estilo computado × 2 viewports, vermelhas primeiro (rota e componente inexistentes) · trava estendida vista reprovando contra `rgba()` num componente e `backdrop-blur` fora do vidro · 404 da vitrine provado contra um **segundo servidor** sem a flag |

Com o Docker resolvido (§ abaixo), o **E0-07** e o **E0-08** deixaram de estar bloqueados. Com o E0-09 verde, a **trilha T3 (design system) também está destravada** — ela dependia só do Playwright configurado.

O E0-08 fecha o nível `pipeline`: existe **um teste real de cada nível** (`unit`, `db`, `rls`, `pipeline`, E2E), e o E0-11 os colocou para rodar no CI — a primeira das oito provas do §12 do plano está fechada.

---

## ~~Bloqueio: Docker não sobe~~ — RESOLVIDO em 2026-08-02

Fica registrado porque o diagnóstico é contraintuitivo e o sintoma pode voltar.

**O que parecia:** `wsl --install` respondia "o sistema não pode encontrar o arquivo especificado", então parecia que faltava instalar o WSL.

**O que era:** o WSL estava instalado **pela metade**. Tudo o que se costuma culpar estava certo — recursos `Microsoft-Windows-Subsystem-Linux` e `VirtualMachinePlatform` habilitados, pacote da Store `WindowsSubsystemForLinux` 2.6.2.0 registrado e íntegro, serviço `WSLService` rodando, `HypervisorPresent = True`. O erro real só apareceu chamando o executável direto em vez de pelo atalho do PATH:

```
Wsl/CallMsi/ERROR_FILE_NOT_FOUND
```

`CallMsi` é a pista. O WSL tem duas metades: o pacote da Store e um **MSI** que popula `C:\Program Files\WSL\`. O registro dizia que o MSI estava instalado (2.6.2.0), mas a pasta tinha só `wsldeps.dll`, `wslservice.exe` e `wslserviceproxystub.dll` — faltava o `wsl.exe` e todo o resto do payload. Provável resíduo da instalação cancelada no UAC em 2026‑04‑20. E o `wsl --install` não conseguia se consertar porque **o binário que faltava era justamente o que ele precisa executar**.

**O conserto (aplicado):**

```powershell
winget install --id Microsoft.WSL -e --force --accept-package-agreements --accept-source-agreements
```

O `--force` é indispensável: sem ele o winget vê o 2.6.2.0 como "já instalado" e sai sem fazer nada — que era exatamente o problema. Resultado: WSL 2.7.11.0, kernel 6.18.33.2-2, `C:\Program Files\WSL` completo. **Não foi preciso reiniciar** (o `VirtualMachinePlatform` já estava ligado antes).

Depois disso o Docker Desktop ainda não subia, por dois motivos independentes:

1. os processos do Docker Desktop tinham ficado presos em estado de falha e nem o `docker desktop restart` os matava (`context deadline exceeded`) — foi preciso `Stop-Process -Force`;
2. `com.docker.service` estava **Stopped** com StartMode **Manual**. Passou para Automatic e foi iniciado (precisa de elevação).

**Estado final:** `docker ps` responde, daemon 29.6.2 (server e client). Docker Desktop está em `D:\docker\Programa\` — fora do caminho padrão, útil saber se precisar mexer de novo.

---

## Checklist de desenvolvimento

Os quadros de "Onde paramos" são o **registro** — item, estado e prova executada. Esta seção é a **lista de trabalho**: o que falta, na ordem em que é executável. O que já fechou aparece aqui só como uma linha; a prova mora lá em cima.

### O ritual de cada item (não muda)

- [ ] Branch a partir da `main` atualizada (o ruleset é `strict`)
- [ ] Teste escrito **primeiro** e visto **vermelho pelo motivo certo** — falha por privilégio ou rota ausente não é prova (decisão 16)
- [ ] Implementar até o verde, sem escrever nada que nenhum teste exija
- [ ] `ruff check` · `lint-imports` · `pytest` do nível tocado — ou `lint`/`typecheck`/`build` + `pnpm e2e` no hub
- [ ] Mexeu em pixel: `update-baselines` **no runner** com `mode=all` (decisão 40), `gh run download`, commit local (decisão 34)
- [ ] **Revisar a imagem da baseline**, nos dois temas — duas vezes seguidas ela achou defeito que teste objetivo nenhum pegaria (decisões 41 e 45)
- [ ] PR → 4 checks verdes → merge → atualizar este arquivo (estado, prova e decisões novas)

### Concluído

- [x] **Ambiente da máquina** — WSL reinstalado, Docker de pé, `uv`/`pnpm`/`supabase` instalados (tabela abaixo)
- [x] **Trilha T1** — E0-01 a E0-05, esqueleto do monorepo (`eacddb3`)
- [x] **Trilha T2** — E0-06 a E0-12: relógio injetável, migration 0001 + `rls`, pgmq real, jornada E2E, `pr.yml`, `main.yml`, provas N1/N2/N4, `main` protegida por ruleset
- [x] **Trilha T3 — completa (9 de 9)** — E0-13 tokens · E0-14 vidro · E0-16 vitrine · E0-17 harness visual · E0-15 L1/L2/L3/L4 · E0-18 prova N3

### A fazer — E1 · steel thread do motor (não depende da T4)

- [x] **PR-0 · fatia do schema** (#19) — 7 tabelas, schema `internal`, fábricas, 2 suítes de segurança · 3 sabotagens com raio exato
- [x] **A1 + A2 · ingestão e contadores** (#21) — 5 sabotagens dirigidas · **achado:** a trava contra `max(seq)+1` não existia e foi criada
- [x] **PR 2b · cenários 1, 3 e 8** (#27) — rajada de 5 vira exatamente 1 resposta (por estado) · reentrega arquivada sem segunda geração · o laço consome na lista exata do schedule do unit · **o conserto da starvation entrou como commit 1, vermelho primeiro**
- [x] **PR 2a · composição real + harness de kill** — app.py vira o processo único do ADR-1/2 (coalescer + 2 workers + sender + heartbeat), o esqueleto do E0-08 morre e suas propriedades sobrevivem re-miradas no EngineLoop · FakeChannel com o banco como IPC · harness Popen com smoke próprio · otel no job · decisão do idioma (53)
- [x] **A5 · claim_outbox_batch + desfechos** — 15 asserções · 3 sabotagens (a do DEFINER derruba os 3 testes que dependem do EXECUTE do sender, colateral honesto) · linha devolvida completa, sender sem segunda query
- [x] **A3 · lease + CAS estendido** — 17 asserções, cada condição do CAS quebrada **isoladamente** · 5 sabotagens · `tenant_id` passa a viajar no job (decisão registrada)
- [x] **A4 · coalescer** — transação única, `SKIP LOCKED`, `generation++` · 9 asserções · 2 sabotagens · **correção do plano:** sem `SKIP LOCKED` não há job duplicado, há bloqueio (provado)
- [x] **Unidade 4 · regras do queueing** — backoff/jitter, classificação, weighted polling, promoção por idade, limites por fila · nível `unit` em 0,5s · acaso injetado com trava de fitness própria

- [x] **Fase 0 · plano detalhado** — `docs/plano-e1-steel-thread.md`: fatia do schema, decisão do canal com rota B, escopo negativo
- [ ] **Fase 1 · especificação vermelha** — A1 ingest_webhook · A2 contadores · A3 lease/CAS · A4 coalescer · A5 claim_outbox_batch · cenários 1–10 · regras do queueing
- [ ] **Fase 2 · implementação** — migrations 0003+ → ingest_webhook() → filas restantes → queueing → coalescer → lease/CAS → outbox+sender → canal com resposta fixa → heartbeat
- [ ] **Fase 3 · provas do marco** — abandono real chega no WhatsApp · kill -9 não perde nada · heartbeat ≤ 3 min

### A fazer — Trilha T4 (bloqueada nos pré-requisitos do Bruno)

- [ ] **B-4 · disparar o gap-check agora** — verificação Meta **incluindo Embedded Signup**, números de teste, webhooks das lojas dev nas 3 plataformas, instância Evolution. Não bloqueia o E0, mas é o maior risco de calendário do projeto
- [ ] **B-1** VPS de staging · **B-2** Logfire + write token · **B-3** Grafana Cloud (OTLP, instance ID, token, IRM) · **B-5** ambiente Supabase de staging (recomendação: segundo projeto)
- [ ] **E0-19** compose runtime + Alloy, credenciais só por `sys.env()`, redação de PII já no processor
- [ ] **E0-20** módulo `obs/`; teste que reprova sem `service.name` / `deployment.environment`
- [ ] **E0-21** mesmo `trace_id` no Logfire **e** no Tempo
- [ ] **E0-22** primeiro segredo no padrão ADR-11 + teste `db` dos dois sentidos (executável por um role, negada ao outro)
- [ ] **E0-23** deploy de staging: migrations → edge functions → runtime (desligamento gracioso) → fumaça

### Placar das oito provas do §12 — 7 de 8

| # | Prova | Estado |
|---|---|---|
| 1 | Um teste de cada nível verde no CI (`unit`, `db`, `rls`, `pipeline`, E2E) | ✅ E0-11 |
| 2 | Componente na regressão visual, desktop **e** mobile | ✅ E0-17 |
| 3 | Mesmo `trace_id` no Logfire e no Grafana Cloud | ⬜ E0-21 (bloqueada em B-2/B-3) |
| 4 | Gates bloqueantes como checks obrigatórios antes da primeira feature | ✅ E0-10 |
| 5 | N1 — quebra de fronteira de módulo reprova | ✅ |
| 6 | N2 — SQL fora da camada de repositório reprova | ✅ |
| 7 | N3 — alteração de componente reprova a regressão nos 2 viewports | ✅ E0-18 |
| 8 | N4 — leitura cross-tenant reprova a suíte `rls` | ✅ |

### Antes dos próximos marcos

- [ ] **Antes do E1** — as duas cobranças anotadas no E0-08 (revoke por fila nova; `archive()` vira sinal na dedup), listadas no fim deste arquivo. O E1 abre escrevendo **primeiro** as suítes A1/A2 de DB e os cenários 1–10 de pipeline
- [ ] **Antes do E2** — definir o LLM do agente
- [ ] **Antes do E4** — escrever `core/formulario-perguntas.md` (é citado no `CLAUDE.md` e não existe); atualizar `core/telas-da-aplicacao.md` para o design; decidir o mobile (recomendação: frames só do wizard, inbox e dashboard)
- [ ] **Antes do E5** — fechar a receita light do vidro para `chrome`/`overlay`, os valores light que faltam (decisão 45) e o ghost de marca. O peso disso mudou de recomendação para condição: **os quatro lotes do E0-15 tiveram defeito de light pego só na revisão de imagem** (L1 contraste do erro · L2 ilegibilidade generalizada · L4 conversa e menu · L3 navegação e tabela) — o dark foi desenhado, o light está sendo derivado na implementação, e cada derivação errada custa um ciclo inteiro de recaptura. Completar a seção 12 no Claude Design e reconciliar os tokens light contra ela **antes** de o E5 abrir as 19 telas do hub
- [ ] **Antes do dashboard do E5** — os componentes da seção 09 que ficaram fora do L3 por escopo declarado (PR #14): **KPI, sparkline e barra de progresso**. O dashboard (B1) consome os três; sem este registro o corte só existia no corpo do PR

Duas regras que não mudam: **a `main` é protegida** — nada entra sem PR com os quatro checks verdes, o fluxo é branch → PR → gate verde → merge; e **nada disso toca o projeto hospedado**, que segue sem migration aplicada até o B-5 estar decidido.

---

## Ambiente desta máquina

| Ferramenta | Versão | Origem |
|---|---|---|
| git | 2.49.0 | pré-existente |
| node | 22.17.1 | pré-existente |
| gh | 2.90.0 | pré-existente |
| pnpm | 11.18.0 | instalado nesta sessão (`npm i -g`) |
| uv | 0.11.32 | instalado nesta sessão (winget, escopo de usuário) |
| supabase CLI | 2.111.0 | instalado nesta sessão (`npm i -g`) |
| docker | 29.6.2 | Docker Desktop em `D:\docker\Programa\` (fora do caminho padrão) |
| WSL | 2.7.11.0 · kernel 6.18.33.2-2 | reinstalado nesta sessão (winget `--force`) |

Projeto Supabase hospedado: `agents-worder` / `jmzsxwtflxsrdfjkuusi`, sa-east-1, Postgres 17.6.1, **sem nenhuma migration aplicada**. `pgmq` 1.5.1 e `vector` 0.8.2 disponíveis; `supabase_vault` 0.3.1 já instalado.

---

## Decisões tomadas durante a execução (não estavam no plano)

Ficam registradas aqui porque mudam como o código se comporta:

1. **Fontes pelo pacote `geist`**, não `next/font/google` — a fonte passa a ser fixada pelo lockfile em vez de baixada no build. Sem isso, a tipografia varia entre a máquina que grava a linha de base visual e a que compara.
2. **`ignoreSnapshots` quando `CI` não está setado** — localmente as jornadas rodam e a comparação visual é pulada. O padrão do Playwright é gravar a baseline ausente e falhar, que é exatamente como uma captura local vira contrato.
3. **O nível do teste vem do diretório** (`runtime/tests/conftest.py`). Só `rls` é marcador manual, porque a suíte de vazamento mora dentro de `tests/db/`.
4. **`maxDiffPixelRatio` pequeno mas não zero** (0.002) — `backdrop-filter` não é determinístico ao pixel. Se o ruído voltar, a saída é capturar o vidro sobre fundo sólido de teste, **não** afrouxar o limite.
5. **`CLAUDE.md` estava duplicado** (linhas 1–58 eram uma cópia antiga e menor da 59–158). Consolidado.
6. ~~A trava de SQL fora da camada de repositório ainda não existe.~~ **Feito no E0-06** (`runtime/tests/unit/test_no_sql_outside_repository.py`): é teste-fitness de nível `unit`, não job de lint. O `docs/plano-e0-fundacao.md` §E0-10 já foi corrigido — o job `sql-lint` deixou de existir.
7. **Detecção por AST, não por regex**, nas duas travas do E0-06. Uma docstring que cita `SELECT max(seq)+1` não é violação; `from time import sleep as nap; nap(30)` é. Cada detector carrega os próprios testes, para que a trava não apodreça em decoração que sempre passa.
8. **`FrozenClock` mora em `runtime/tests/support/`, não no pacote do runtime.** Duplo de teste não viaja na imagem de produção. O `agents_runtime/clock.py` é o único arquivo autorizado a ler o relógio real — é assim que a trava está escrita.
9. **`runtime/tests/` virou pacote** (`__init__.py` em `tests/`, `tests/unit/`, `tests/support/`). Sem isso, `tests.support` não importa e dois arquivos de teste com o mesmo nome em níveis diferentes colidiriam.
10. **A home do hub é placeholder deliberado.** O E0 não entrega tela desenhada; a jornada do E0-09 afirma só `data-testid="hub-home"`, título, `lang` e ausência de rolagem horizontal — marcadores escolhidos para sobreviver à reconstrução da página sobre o design system na T3, sem editar o teste. Os assets do `create-next-app` foram removidos junto.
11. ~~**Nada foi empurrado para o GitHub ainda.**~~ **Resolvido em 2026-08-02:** `main` e `e0-foundation` empurradas, PR #1 mergeado, gate de pé. Ver decisões 26–28.
12. **A stack local sobe enxuta.** `supabase start -x "..."` com 12 dos 14 containers excluídos — sobram Postgres e GoTrue, que é tudo que as suítes `db`/`rls` tocam. O GoTrue fica porque `profiles.user_id` e `memberships.user_id` referenciam `auth.users`. Comando completo na seção Commands do `CLAUDE.md`. **A lista precisa de aspas:** o PowerShell interpreta valor separado por vírgula como array e passa só o primeiro nome, silenciosamente.
13. **As policies vão na mesma migration das tabelas**, não numa migration seguinte. Tabela que existe por uma migration que seja com GRANT e sem policy foi legível cross-tenant em algum ponto da história do schema. O ciclo vermelho→verde aconteceu de verdade — a suíte rodou contra as tabelas com GRANT e sem policy e foi vista retornando linhas do tenant errado —, mas a prova mora na mensagem do commit `cc6406a`, não numa migration permanentemente insegura.
14. **Roles `nologin`.** Pool separado exige senha, e senha em migration commitada é segredo vazado; o grant de login fica fora de banda, por ambiente. `grant worker_role, sender_role to postgres` existe para o `postgres` conseguir assumi-los (`SET ROLE`) — não concede nada aos roles.
15. **Revisão de código pós-E0-07 (2026-08-02) fechou cinco achados**, cada um com teste vermelho visto primeiro (77 verdes ao final): `current_app_tenant_id()` agora falha fechado também para valor **imparseável** (plpgsql + `exception when invalid_text_representation` — antes o cast `::uuid` virava erro em toda query da conexão, e o comentário prometia NULL); `tenants.updated_at` passou a mover por trigger (`touch_updated_at`, reutilizável pelas próximas tabelas); o detector de relógio rastreia alias de módulo/classe (`import time as t`, `from datetime import datetime as dt`); nasceu `agents_runtime/__main__.py` (o CMD da imagem existia sem alvo — o container buildava e morria no primeiro start) com fitness test de que o alvo do CMD existe; e o harness de `db`/`rls` passou a escopar `app.tenant_id` e `request.jwt.claims` por **transação** (`is_local => true`), espelhando a disciplina `SET LOCAL` que o driver real usará no E0-08. A migration 0001 foi editada em vez de criar uma segunda — nada foi implantado em lugar nenhum, mesma lógica da decisão 13.
16. **O primeiro vermelho do E0-07 foi rejeitado.** 11 das 16 falhas eram `permission denied to set role` e `permission denied for table` — isso testa o GRANT, não a policy. Só depois de conceder privilégio de tabela aos três caminhos o vazamento pôde acontecer de fato. Vale a regra geral: **falha por privilégio ausente não é prova de RLS.**
17. **Uma fila, não quatro.** A migration 0002 cria `pgmq` e só `q_inbound`. As outras três (`q_domain_events`, `q_scheduled`, `q_evals`) e os DLQs nascem no E1 junto do weighted polling que lhes dá sentido — fila que ninguém lê é fila que ninguém testa.
18. **O handler do E0 recusa o job, não o engole.** O `__main__` passa um handler que levanta `NotImplementedError`: o processo sobe, faz polling de fila vazia e morre alto se aparecer trabalho de verdade. Um no-op que arquivasse o que lesse seria exatamente o modo de falha que este marco existe para excluir — job que some sem ninguém ver. Pela mesma razão, o laço **não** arquiva quando o handler falha (backoff/DLQ são E1); o teste afirma que a mensagem continua na fila.
19. **O laço só pode parar entre jobs.** `run()` checa o desligamento no topo do ciclo; da reivindicação em diante, arquivar é a única saída. Assim "parar de reivindicar, terminar o que está na mão" não precisa decidir o que fazer com meio job — e deploy é rotina, não exceção.
20. **A suíte de exposição da fila foi provada nos dois sentidos.** Ela passou de primeira, o que não prova nada — então foi sabotada: `grant select on pgmq.q_q_inbound to authenticated` reprovou **exatamente** o caso `authenticated` (e só ele), e `revoke insert ... from worker_role` reprovou o teste positivo. Banco restaurado com `supabase db reset` em seguida. Vale como regra: **teste de fronteira que nunca foi visto vermelho é decoração.**
21. **A fila é dirigida como `worker_role` nos testes, não como superusuário.** O fixture faz `set role worker_role` antes de entregar a `PgmqQueue`. Rodar como `postgres` passaria com qualquer grant, e o primeiro privilégio a faltar em produção seria um que a suíte nunca exercitou.
22. **Isolamento do nível `pipeline` é por purga, não por prefixo.** Fila é estado compartilhado, não linha: cada teste começa e termina com `q_inbound` vazia. O prefixo por run que o nível `db` usa não se aplica.
23. **`psycopg` virou dependência de produção** (era só de desenvolvimento). A imagem instala com `--no-dev`; sem a mudança, o container subiria sem o driver que o laço agora importa.
24. **Windows precisa do selector loop** para as conexões assíncronas do psycopg. O `tests/pipeline/conftest.py` implementa o hook `pytest_asyncio_loop_factories` (a API antiga, `event_loop_policy`, está deprecada no pytest-asyncio 1.x) e só na máquina de desenvolvimento — em Linux o selector já é o padrão.
25. **Revisão do E0-08 fechou dois achados.** (a) O contrato "só a camada de repositório alcança o banco" listava nove módulos e **não** listava `queueing` — que acabara de ganhar código. O furo foi confirmado antes do conserto: com `import agents_runtime.repository.driver` plantado no `loop.py`, o `lint-imports` respondia "3 kept, 0 broken". Com `agents_runtime.queueing` na lista, o mesmo import reprova nomeando arquivo e linha. `agents_runtime.app` fica **fora** da lista de propósito: a raiz de composição é quem constrói os pools e os entrega aos outros. (b) O `revoke all on all functions in schema pgmq` saiu: as funções do pgmq têm EXECUTE via PUBLIC e rodam como quem chama, então revogar dos três roles nomeados não removia nada e só parecia proteção. A fronteira real é o privilégio de **tabela** — que é o que a sabotagem provou e o que a suíte segura. A migration 0002 foi editada no lugar (nada implantado em lugar nenhum, mesma lógica das decisões 13 e 15).
26. **O repositório passou a ser público.** Conta Free + repo privado **não tem** proteção de branch nem rulesets — as duas APIs respondem `403 Upgrade to GitHub Pro or make this repository public`. Ou seja, a definição de pronto do E0-10 ("os quatro jobs são checks obrigatórios") era inalcançável sem mudar algo. As saídas eram: assinar o Pro (US$4/mês), tornar público, ou abrir mão do bloqueio. **Bruno escolheu tornar público**, ciente de que expõe a especificação inteira em `core/` — a recomendação registrada era outra. Antes da mudança o histórico foi varrido: nenhum `.env`, nenhuma chave, nenhum JWT em commit algum. Fica exposto o *ref* do projeto Supabase hospedado em dois documentos — não é credencial (sem a anon key o endpoint devolve 401), mas é redigível se um dia incomodar.
27. **A `main` é protegida por ruleset** (`main protegida`, id 20223317): 4 checks obrigatórios, PR obrigatório com zero aprovações (dev solo), sem force-push, sem deletar a branch, e `strict` — a branch precisa estar atualizada com a `main`. **Não há bypass configurado**, nem para o dono: o teste de que a proteção existe foi um push direto na `main` ser recusado. Se algum dia travar demais, o ajuste é adicionar bypass ao papel de admin — nunca desligar a regra.
28. **O CI achou um bug que a máquina de desenvolvimento não podia achar.** O hook `pytest_asyncio_loop_factories` devolvia `None` fora do Windows, e o pytest-asyncio trata "implementação que recusa" diferente de "sem implementação": `None` é `UsageError`, e os três arquivos de `pipeline` quebraram na coleta em Linux. O conserto foi registrar o hook **só** no Windows (`if sys.platform == "win32"` em volta do `def`). É o argumento inteiro a favor do gate, no primeiro dia dele: verde local em Windows não é verde.
29. **Cinco decisões de tradução no E0-13** — pontos onde o design e o CSS não têm equivalência literal, todos registrados no próprio `globals.css`: (a) **`--color-surface-solid` é um slot só.** O design chama o terceiro neutro de "solid" no dark (#1A1B20, mais claro que o fundo) e "sunken" no light (#E9E7E3, mais escuro) porque a elevação inverte — mas é a mesma posição da paleta, e posição só pode ter um nome. (b) **`--radius-pill` é 9999px**, e o design desenha 99px: mesma forma abaixo de 198px de altura, e continua redondo acima. (c) **O breakpoint virou `--breakpoint-desk: 860px`** porque breakpoint do Tailwind é `min-width` e o design fala em "< 860": o não-prefixado é o layout mobile, `desk:` é 860 pra cima. (d) **`--spacing: 2px`**, base 2 do design, o que faz a escada 4/8/14/18/32 ser exprimível exatamente — com apelidos nomeados (`p-card`, `gap-cards`) para o componente não escrever `p-9`. (e) **Light não escuta `prefers-color-scheme`**, só `data-theme`: o dark é a cara do produto, e quem abre o hub numa máquina em light deve ver o que o design mostra até pedir o contrário.

---

30. **A vitrine é `/design`, não `/_design`.** O plano pedia `_design`; no App Router do Next, pasta com prefixo `_` é privada e **sai do roteamento** — a rota simplesmente não existiria. A proteção real não é o nome: é a flag `DESIGN_SHOWCASE` avaliada no servidor, com `force-dynamic` para a decisão ser de runtime e não de build. E isso virou asserção: o Playwright sobe **dois** servidores, o segundo sem a flag, e afirma 404 contra ele. "Fora do build de produção" deixou de ser promessa.
31. **Três achados do pipeline de CSS no E0-14.** (a) Escrever `-webkit-backdrop-filter` à mão fez o Lightning CSS **descartar** a propriedade sem prefixo — o vidro saiu do build sem `backdrop-filter` nenhum, e o teste pegou. A regra: não escrever prefixo à mão, o build prefixa. (b) O build reescreve `rgba()` para hex de 8 dígitos, o que **quantiza o alfa para um byte** (`0.075` → `#ffffff13` → 0.0745). É determinístico — os doze valores do vidro batem com `Math.round(alfa × 255)` — então a comparação canoniza os dois lados em `e2e/support/css.ts` em vez de o contrato passar a ser escrito em forma minificada. (c) O Next 16 só permite **um `next dev` por diretório de build** (o lock é da pasta, não da porta), então o segundo servidor tem o seu, via `distDir` por variável de ambiente.
32. **A trava de cor virou `no-loose-colour.mjs`** e cobre agora `rgb/rgba/hsl/hwb/lab/lch/oklab/oklch/color(` além de hex, mais uma segunda regra: `backdrop-filter`/`backdrop-blur` **só** no arquivo de tokens — nem no `glass.tsx`, que não precisa de nenhum dos dois porque a receita é CSS e o componente só decide nível e aninhamento. Empilhar vidro por utility solta é o mesmo bug que o contexto previne, só que por fora do componente. `color-mix()` fica liberado de propósito: ele compõe tokens, não inventa cor.
33. **O vidro em light está parcialmente sem fonte.** A seção 12 do design desenha a receita light do nível **card** e só dele. `chrome` e `overlay` reusam a superfície aprovada do card mantendo blur e raio próprios, e o aninhado em light usa `rgba(23,24,28,0.05)` — a superfície recuada que o design já usa em light (balão recebido, seção 12). É reuso, não cor inventada, mas **é pendência de design**: fechar antes do E5, que constrói o shell do hub.

34. **A baseline visual vem por artefato, não por commit do bot.** O plano previa o workflow commitando os PNGs na branch do PR. Não dá: **push feito com `GITHUB_TOKEN` não dispara workflow** — proteção do GitHub contra recursão —, então o PR ficaria com os quatro checks obrigatórios em "expected" contra um commit sem run nenhum, e sem caminho para merge. A saída é `gh run download` e commit local: a baseline continua **nascendo no runner** (que é o requisito real), continua entrando no diff do PR como imagem, e o push humano continua disparando o gate. A alternativa seria um PAT com escrita guardado nos segredos do repositório — custo de segurança que não se paga por conveniência.
35. **Sair não-zero ao gravar baseline é o comportamento correto do Playwright**, não um defeito: ele reprova a run sempre que escreve um snapshot que não existia, para que nenhum gate aceite pixel novo em silêncio. Como isso é o propósito do `update-baselines`, o código de saída não é o sinal ali — o artefato é, e `if-no-files-found: error` é quem de fato reprova o job quando nada foi capturado. Visto falhando primeiro: o run 30753874300 escreveu os quatro PNGs e mesmo assim pulou o upload.
36. **`workflow_dispatch` só é despachável se o arquivo estiver na branch padrão** — mas a run usa a versão do arquivo da branch escolhida. Por isso o harness entrou em PR separado (#9) antes das asserções que dependem dele (#10), e por isso o conserto da decisão 35 pôde ser testado na própria branch do PR.
37. **Captura por seção, nunca de página inteira.** A vitrine cresce por quatro lotes no E0-15; baseline de página inteira seria invalidada por cada lote e faria cada PR reescrever a evidência dos anteriores. Por seção, cada lote só cria as suas — e o `data-testid` numerado da vitrine é o que torna isso estável.

38. **Cinco variantes de botão, não quatro.** O design desenha dois níveis de danger e eles significam coisas diferentes: o suave para destrutivo **reversível** (pausar agente, cancelar tenant) e o sólido para o **irreversível** (executar purga). Colapsar os dois faria as duas ações lerem igual — e este é o lugar do produto onde elas não podem.
39. **Texto de controle ganhou tokens de papel próprios** (`--text-control-sm|md|lg` = 12/13/14.5, `--text-field-*` = 13.5/12.5/11.5). Os desenhos dos componentes usam tamanhos **entre** os degraus da escala tipográfica da seção 02. Escala que os componentes ignoram em silêncio não é escala; nomear a exceção a mantém visível — e se um dia a escala absorver esses valores, o rename é de token, não de componente.
40. **O limiar de 0.002 é cego para mudança pequena em seção grande.** Descoberto na prática: corrigir a cor do texto de erro no tema light mudou ~600 pixels de uma seção de 743 mil, ou seja 0.0008 — **abaixo** do limiar, e a comparação passou contra a baseline antiga. Duas consequências registradas: (a) depois de qualquer mudança visual deliberada, regravar com `mode=all`, para a baseline ser o render atual e não uma que apenas cabe na tolerância; (b) a mitigação estrutural, se isso incomodar, é capturar unidades menores — por componente em vez de por seção —, **nunca** apertar o limiar, que existe porque `backdrop-filter` não é determinístico ao pixel.
41. **Acessibilidade do L1 corrigiu um defeito que a própria revisão visual encontrou.** O texto de erro herdava o rosa claro do dark e ficava em ~2:1 sobre a superfície light; passou a usar o rosa escuro que o design já emprega no botão danger sólido. E o spinner não girava — agora gira atrás de `prefers-reduced-motion`, que é o mesmo mecanismo que mantém a captura estável (o Playwright renderiza com `reduce`, então a baseline sempre encontra o spinner parado).

42. **Os cinco status do badge são o enum de `tenants.status`**, não uma lista visual paralela: `active · paused · pending_approval · shadow · cancelled`, identificadores em inglês e copy em PT-BR. Um badge com estados que o banco não tem é um badge que mente. O `shadow` é o único tracejado e o único sem ponto — modo shadow é temporário, e a borda diz isso sem legenda.
43. **Alert e Toast diferem por papel, não por estilo.** `role="alert"` interrompe o leitor de tela; `role="status"` espera a vez. É a diferença entre "seu número caiu" e "salvo". O toast é `<Glass level="overlay">` com raio e sombra próprios (o design desenha 16 e um lance mais curto que o de um modal) — composto, não redesenhado, para continuar sob a regra de que vidro não empilha.
44. **O estado vazio ganhou um slot de ação opcional que o design não desenha.** O desenho da seção 11 tem título e descrição e nada mais; `core/telas-da-aplicacao.md` §D3 pede "vazio com orientação". O slot é opcional e recebe o Button do L1 — estado vazio nunca cria botão próprio. Se a orientação virar regra, é o design que ganha o desenho.
45. **A revisão da baseline light do L2 pegou ilegibilidade generalizada** — "ativo", "em aprovação", o título do toast e o estado vazio inteiro apareciam como fantasmas. Causa: a paleta dark põe texto claro sobre tint escuro, e na superfície light o mesmo texto some. Conserto: light mantém **tint e ponto** como canal semântico e escurece o texto, transcrevendo o que o design desenha (a pílula ativa da seção 12 é exatamente `#9A3412` sobre `rgba(249,115,22,0.12)`) e caindo para `--color-fg`/`--color-fg-muted` onde ele não desenha — nunca para um amarelo ou verde escuro inventado. **Duas vezes seguidas a revisão de imagem achou um defeito que nenhum teste objetivo pegaria**; ela é etapa, não formalidade.

46. **`GlassBoundary` é a única saída legítima da regra do vidro.** Toda sobreposição a usa: modal, popover e menu abrem *fora* do que os originou, e o `backdrop-filter` deles amostra a página, não o card. O contexto do React não sabe disso — atravessa portal e top layer igual —, então sem o reset o painel se declara aninhado e entrega o blur. Componente que alcança o `GlassBoundary` sem ser sobreposição está contornando a regra, não aplicando. As **duas metades ficam lado a lado na vitrine**, dentro do mesmo card: testar só uma aceitaria como conserto "desligar a regra".
47. **O modal é o `<dialog>` nativo com `showModal()`** — focus trap, Esc e top layer vêm de graça, e o ônus da prova é de quem quer a dependência. A modalidade é afirmada pelo **`::backdrop`**, que só é renderizado quando aberto modalmente; um `aria-modal="true"` escrito à mão provaria apenas que alguém o digitou.
48. **Dois falsos positivos na trava de cor, ambos consertados com o teste vendo antes.** (a) Ela flagrava `backdrop-filter` escrito num **comentário** — mesma lição dos detectores do E0-06: prosa não é violação, e flagrar comentário ensina a parar de comentar. (b) Flagrava `#4821`, número de pedido na copy, porque quatro dígitos hex são quatro dígitos hex. Em TS a cor passou a exigir contexto de cor (aspas, parêntese ou dois-pontos); em CSS a forma nua continua valendo, senão `border: 1px solid #fff` escaparia.
49. **A ilegibilidade do light é sistêmica, não incidental.** Três lotes seguidos, mesma causa: a paleta dark é branco-sobre-escuro do começo ao fim, e cada componente novo herda isso. A regra que fica: **conferir o tema light antes de gerar baseline é etapa obrigatória de todo lote** — e o conserto é sempre superfície recuada aprovada + texto escuro o bastante, nunca matiz inventado. A causa raiz continua sendo a pendência de design: a seção 12 desenha light para um punhado de componentes.

50. **O status `pending_approval` não existia.** O enum de `tenants.status` na migration 0001 é `onboarding · shadow · active · paused · cancelled`; o L2 tinha inventado `pending_approval` a partir da copy do design ("em aprovação"). Renomeado em componente, tokens, seletores, testids e specs. **Badge que mostra estado que o banco não segura é badge que mente** — e fica a pergunta para o design: "em aprovação" é mesmo o estado `onboarding`, ou é a aprovação de uma versão do agente, que é outra coisa?
51. **O raio da sabotagem N3 foi exatamente onde o componente aparece.** Mudar o padding do botão `md` reprovou só a seção 05 — o botão do estado vazio é `sm` e os do modal só existem com o diálogo aberto. Uma trava que reprovasse seções sem o componente estaria reprovando por ruído, e é isso que a captura por seção (decisão 37) compra.
52. **Uma diferença de 1px sem causa identificada.** Ao regravar as baselines no L3, as seções 10 e 11 mudaram 1px de altura com as imagens visualmente idênticas. Nenhuma regra do L3 toca aquelas seções, e não achei a causa — fica registrado em vez de inventada. Se repetir em outro lote, investigar **antes** de regravar.

53. **A lei do idioma fica; a deriva é que se corrige.** As migrations 0005–0008 e as suítes do E1 derivaram para comentários em português, contra a convenção do CLAUDE.md (código, identificadores, comentários e commits em inglês). Decidido: **a lei permanece** — código novo volta ao inglês a partir do PR 2a; os sobreviventes em português são traduzidos oportunisticamente quando o arquivo for tocado de novo, nunca como um diff atacadista de comentários. Mudar a lei para validar a deriva premiaria a deriva.
54. **O PR 2 foi dividido como pré-autorizado**: 2a = composição + harness (verde), 2b = cenários 1/3/8. A composição sozinha passou das ~800 linhas de produção.
56. **O harness de kill usa `subprocess.Popen`, não o subprocesso do asyncio**: selector loop não gerencia filhos no Windows. Espera é sempre por predicado contra o banco — o banco é o observável que o processo e o teste compartilham, e é o mesmo que o monitoramento de produção vai usar. `terminate()` no Windows é TerminateProcess (não existe sinal limpo), então o smoke do desligamento gracioso roda só no CI Linux, com skip explícito.
57. **O sender só existe quando existe canal.** `AGENTS_CHANNEL` é uma fábrica `module:callable`; ausente, a task do sender nem sobe — explícito, em vez de um sender inventando desfechos contra um canal que não existe. E o FakeChannel grava o envio ANTES de retornar, espelhando o mundo real: o provedor tem a mensagem no momento em que a API aceita, aconteça o que acontecer com o nosso processo depois — é a ordem de que o cenário 10 depende.
58. **A janela de starvation do mapa de crencas** -- achado da revisao do PR 2a, consertado vermelho-primeiro no PR 2b. A crenca so era restaurada quando TODAS as filas servidas estavam quietas; sob rajada sustentada de inbound, uma fila marcada quieta nunca mais era sondada ate a rajada acabar -- um order_paid esperando a rajada inteira para cancelar o funil, exatamente o caso que o ADR-5 existe para impedir, derrotado em silencio sob carga. A promocao por idade nao socorre: promover exige ler, e era a leitura que nao acontecia. Conserto: **a crenca expira a cada janela consumida** (15 turnos) -- custo de no maximo uma leitura vazia por fila por janela, garantia de que nenhuma fila servida fica sem sondagem por mais de uma janela. Registro honesto: o primeiro vermelho foi pelo motivo errado (rajada nao semeada, o laco ficou ocioso ate o timeout); semeado, o vermelho verdadeiro apareceu, o conserto o apagou, e desligar a expiracao trouxe de volta exatamente aquele vermelho. **Vermelho por harness quebrado e vermelho por defeito real sao coisas diferentes, e a diferenca agora esta documentada.**

---

## Pendências que continuam abertas

Do plano (§9 e §11), nenhuma resolvida ainda:

- **B-1** VPS de staging · **B-2** conta Logfire · **B-3** conta Grafana Cloud · **B-4** gap-check Meta/lojas/Evolution · **B-5** decidir o ambiente Supabase de staging (recomendação: segundo projeto).
- **Receita light do vidro para `chrome` e `overlay`** — a seção 12 do design só desenha o nível `card` (decisão 33). Fechar antes do E5.
- **Valores light que faltam no design, lista fechada** (decisão 45). Hoje substituídos por neutros legíveis; o design precisa decidir: (a) **amarelo escuro** para texto de atenção (status `pending_approval`, alerta de atenção); (b) **verde escuro** para texto de sucesso (badge técnico de score); (c) confirmar se `#E11D48` é mesmo o vermelho de texto em light, que foi o que assumi por ser a parada final do botão danger sólido. Sem isso, atenção e sucesso perdem o canal de cor no tema light — o tint e o ponto seguram, mas o texto fica neutro.
- **Ghost em laranja não foi implementado.** A seção 05 desenha dois ghosts — um neutro ("Cancelar", `#A9AAB2`) e um de marca ("Ver todos", `#F97316`). Só o neutro virou variante; o de marca parece link de navegação e não botão, e a decisão de qual é qual pertence ao E5, quando existir uma tela com os dois. Se for botão, é uma variante a mais, não um `className` no lugar de uso.
- **O lote L4 (conversa e sobreposição) precisa resetar o contexto do vidro na fronteira do portal.** A regra de aninhamento viaja por contexto React, o que é correto — inclusive através de server components. Mas um modal ou popover portalado abre *fora* do vidro que o originou e ainda assim herdaria o contexto: se declararia aninhado e perderia o blur indevidamente, sendo que visualmente não está empilhado em nada. O componente de sobreposição tem de envolver seu conteúdo em `InsideGlass.Provider value={false}` na fronteira do portal, com teste dedicado. Anotado no E0-14, para não depender de alguém lembrar no E0-15.
- Telas sem layout mobile; divergências entre o design e `core/telas-da-aplicacao.md`; `core/formulario-perguntas.md` inexistente; LLM do agente indefinido.

Anotadas no E0-08 para serem cobradas no **E1**:

- **Cada fila nova repete a disciplina de revoke** — os `revoke` da migration 0002 valem só para os objetos que existiam quando ela rodou. `q_domain_events`, `q_scheduled`, `q_evals` e os DLQs entram cada um com o seu revoke **e o seu teste**, no mesmo PR que os cria.
- **O retorno de `archive()` vira sinal.** Hoje é descartado, e com um consumidor só isso é correto. Quando a dedup de jobs existir, `False` significa "outro consumidor já terminou este job" — que é exatamente o que a dedup precisa observar, não ignorar.

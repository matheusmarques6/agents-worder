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
| E0-04 supabase (config) | ⚠️ parcial | `config.toml` gerado e ajustado; **`supabase start` nunca foi executado — falta Docker** |
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

Com o Docker resolvido (§ abaixo), o **E0-07** e o **E0-08** deixaram de estar bloqueados. Com o E0-09 verde, a **trilha T3 (design system) também está destravada** — ela dependia só do Playwright configurado.

O E0-08 fecha o nível `pipeline`: existe agora **um teste real de cada nível** (`unit`, `db`, `rls`, `pipeline`, E2E), que é a primeira das oito provas do §12 do plano — falta só rodá-los no CI.

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

## A retomada, em ordem

1. **Trilha T3** (E0-13 tokens → E0-18) — é o próximo bloco. Fecha também a **N3**, a única prova negativa que falta.
2. **Trilha T4** (E0-19 → E0-23) — bloqueada em B-1/B-2/B-3.

A partir daqui **a `main` é protegida**: nada entra sem PR com os quatro checks verdes. O fluxo de todo item passa a ser branch → PR → gate verde → merge.

Lembrete que não muda: **nada disso toca o projeto hospedado.** Ele segue sem migration aplicada até o B-5 (ambiente de staging) estar decidido.

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

---

## Pendências que continuam abertas

Do plano (§9 e §11), nenhuma resolvida ainda:

- **B-1** VPS de staging · **B-2** conta Logfire · **B-3** conta Grafana Cloud · **B-4** gap-check Meta/lojas/Evolution · **B-5** decidir o ambiente Supabase de staging (recomendação: segundo projeto).
- Telas sem layout mobile; divergências entre o design e `core/telas-da-aplicacao.md`; `core/formulario-perguntas.md` inexistente; LLM do agente indefinido.

Anotadas no E0-08 para serem cobradas no **E1**:

- **Cada fila nova repete a disciplina de revoke** — os `revoke` da migration 0002 valem só para os objetos que existiam quando ela rodou. `q_domain_events`, `q_scheduled`, `q_evals` e os DLQs entram cada um com o seu revoke **e o seu teste**, no mesmo PR que os cria.
- **O retorno de `archive()` vira sinal.** Hoje é descartado, e com um consumidor só isso é correto. Quando a dedup de jobs existir, `False` significa "outro consumidor já terminou este job" — que é exatamente o que a dedup precisa observar, não ignorar.

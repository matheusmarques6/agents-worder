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

Próximo item verificável: **E0-10** (`pr.yml`) — mas ver a pergunta em aberto no fim deste arquivo. O **E0-07** e o **E0-08** estão **bloqueados pelo Docker** (§ Bloqueio abaixo) — dá para escrever, não dá para ver vermelho e fechar em verde localmente.

Com o E0-09 verde, a **trilha T3 (design system) está destravada** — ela dependia só do Playwright configurado.

---

## Bloqueio ativo: Docker não sobe porque o WSL não existe

Diagnóstico desta sessão (o reboot não resolveu porque a causa era outra):

- Docker Desktop **está instalado**, fora do caminho padrão: `D:\docker\Programa\`. O CLI responde (`docker 29.6.2`) e a GUI abre.
- `com.docker.service` existe mas fica **Stopped** (StartMode Manual).
- **Causa raiz:** `wsl --version`, `wsl -l -v` e `wsl --status` falham todos com "o sistema não pode encontrar o arquivo especificado". O `wsl.exe` existe em `System32`, mas o WSL em si não está instalado/habilitado — e o Docker Desktop no Windows 11 Home depende do backend WSL2.
- O último log de instalação (`%LOCALAPPDATA%\Docker\install-log.txt`, 2026‑04‑20) mostra o instalador **cancelado no prompt do UAC**.

**Ação do Bruno (precisa de elevação, não dá para fazer daqui):** abrir o PowerShell **como Administrador** e rodar `wsl --install`, reiniciar, e então abrir o Docker Desktop. Depois disso, `docker ps` responde e o `supabase start` volta a ser possível.

---

## A retomada, em ordem

1. **`supabase start`** (na raiz do repo), assim que o Docker subir — primeira execução baixa as imagens, demora. É aqui que se fecha o risco R2 do plano: confirmar que a imagem local traz **pgmq** e **pgvector**. Se não trouxer, vale o plano B do E0-04 (serviço Postgres próprio no CI com as extensões).
2. **E0-09** — jornada Playwright na home do hub, nos dois viewports. **Não depende de Docker**; é o próximo item a executar enquanto o WSL não existe. Destrava a trilha T3.
3. **E0-10/E0-11** — `pr.yml` e `main.yml`. Também não dependem do Docker local: o Postgres efêmero é serviço do GitHub Actions. Ter o CI de pé é o caminho alternativo para ver o E0-07 vermelho→verde sem Docker na máquina.
4. **E0-07** — migration `0001` (`tenants`, `profiles`, `memberships` conforme `core/dicionario-de-dados.md` §1.1–1.3), roles `worker_role`/`sender_role` sem BYPASSRLS, políticas RLS nos três caminhos. O teste de vazamento é escrito **antes** da policy, para vazar de verdade e só então fechar. **Não aplicar no projeto hospedado** — ele é o único que existe e o B-5 (ambiente de staging) segue indefinido.
5. **E0-08** pgmq · **E0-12** as quatro provas negativas (a N1 e a N2 já têm mecanismo pronto e visto reprovando localmente; falta o registro em PR descartável).

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
| docker CLI | 29.6.2 | `D:\docker\Programa\resources\bin\docker.exe` |
| **daemon docker** | — | **bloqueado — WSL ausente (ver § Bloqueio)** |

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
11. **Nada foi empurrado para o GitHub ainda.** Os três commits vivem só na `e0-foundation` local. O `pr.yml` só pode ser verificado depois de um push — decisão do Bruno.

---

## Pendências que continuam abertas

Do plano (§9 e §11), nenhuma resolvida ainda:

- **B-1** VPS de staging · **B-2** conta Logfire · **B-3** conta Grafana Cloud · **B-4** gap-check Meta/lojas/Evolution · **B-5** decidir o ambiente Supabase de staging (recomendação: segundo projeto).
- Telas sem layout mobile; divergências entre o design e `core/telas-da-aplicacao.md`; `core/formulario-perguntas.md` inexistente; LLM do agente indefinido.

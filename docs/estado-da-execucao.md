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

**Trilha T2 (harness + CI) — não iniciada.** Próximo item: **E0-06**.

---

## A retomada, em ordem

1. **Depois do reboot, confirmar o Docker:** `docker --version` e `docker ps`.
2. **Confirmar que o `uv` entrou no PATH.** Ele foi instalado por winget e o PATH do usuário já tem a pasta, mas os shells desta sessão não pegaram a mudança — o reinício resolve. Se `uv --version` falhar, o binário está em
   `C:\Users\mathe\AppData\Local\Microsoft\WinGet\Packages\astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe\uv.exe`.
3. **`supabase start`** (na raiz do repo) — primeira execução baixa as imagens, demora. É aqui que se fecha o risco R2 do plano: confirmar que a imagem local traz **pgmq** e **pgvector**. Se não trouxer, vale o plano B do E0-04 (serviço Postgres próprio no CI com as extensões).
4. **E0-06** — relógio injetável (`Clock`) + primeiro teste unitário verde + o teste-fitness que proíbe `datetime.now()`/`time.time()`/`sleep` direto em código de domínio. Não depende de Docker.
5. **E0-07** — migration `0001` (`tenants`, `profiles`, `memberships` conforme `core/dicionario-de-dados.md` §1.1–1.3), roles `worker_role`/`sender_role` sem BYPASSRLS, políticas RLS nos três caminhos. O teste de vazamento é escrito **antes** da policy, para vazar de verdade e só então fechar.
6. **E0-08** pgmq · **E0-09** E2E · **E0-10/11** workflows · **E0-12** as quatro provas negativas.

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
| **docker** | — | **pendente — motivo do reboot** |

Projeto Supabase hospedado: `agents-worder` / `jmzsxwtflxsrdfjkuusi`, sa-east-1, Postgres 17.6.1, **sem nenhuma migration aplicada**. `pgmq` 1.5.1 e `vector` 0.8.2 disponíveis; `supabase_vault` 0.3.1 já instalado.

---

## Decisões tomadas durante a execução (não estavam no plano)

Ficam registradas aqui porque mudam como o código se comporta:

1. **Fontes pelo pacote `geist`**, não `next/font/google` — a fonte passa a ser fixada pelo lockfile em vez de baixada no build. Sem isso, a tipografia varia entre a máquina que grava a linha de base visual e a que compara.
2. **`ignoreSnapshots` quando `CI` não está setado** — localmente as jornadas rodam e a comparação visual é pulada. O padrão do Playwright é gravar a baseline ausente e falhar, que é exatamente como uma captura local vira contrato.
3. **O nível do teste vem do diretório** (`runtime/tests/conftest.py`). Só `rls` é marcador manual, porque a suíte de vazamento mora dentro de `tests/db/`.
4. **`maxDiffPixelRatio` pequeno mas não zero** (0.002) — `backdrop-filter` não é determinístico ao pixel. Se o ruído voltar, a saída é capturar o vidro sobre fundo sólido de teste, **não** afrouxar o limite.
5. **`CLAUDE.md` estava duplicado** (linhas 1–58 eram uma cópia antiga e menor da 59–158). Consolidado.
6. **A trava de SQL fora da camada de repositório** ainda **não existe**. O plano previa um job de lint; a intenção agora é implementá-la como teste unitário de fitness junto com o E0-06, para rodar no gate `unit` e ser reproduzível localmente. **Atualizar o E0-10 do plano quando isso for feito.**

---

## Pendências que continuam abertas

Do plano (§9 e §11), nenhuma resolvida ainda:

- **B-1** VPS de staging · **B-2** conta Logfire · **B-3** conta Grafana Cloud · **B-4** gap-check Meta/lojas/Evolution · **B-5** decidir o ambiente Supabase de staging (recomendação: segundo projeto).
- Telas sem layout mobile; divergências entre o design e `core/telas-da-aplicacao.md`; `core/formulario-perguntas.md` inexistente; LLM do agente indefinido.

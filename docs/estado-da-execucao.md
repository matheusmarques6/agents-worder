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

Com o Docker resolvido (§ abaixo), o **E0-07** e o **E0-08** deixaram de estar bloqueados. Com o E0-09 verde, a **trilha T3 (design system) também está destravada** — ela dependia só do Playwright configurado.

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

1. **E0-08** — pgmq real (`send` → `read(vt)` → `archive`) + esqueleto do laço do runtime, desligando graciosamente sem deixar mensagem em limbo. É o nível `pipeline`, ainda sem nenhum teste.
2. **E0-10/E0-11** — `pr.yml` e `main.yml`. Precisa de push (ver decisão 11).
3. **E0-12** — registrar as quatro provas negativas em PR descartável. As quatro já foram vistas reprovando **localmente** (N1 pelo import-linter, N2 e a do relógio no `-m unit`, N4 no `-m rls`); falta só a N3, que depende da trilha T3, e o registro formal com link de run.
4. **Trilha T3** (E0-13 tokens → E0-18) — destravada desde o E0-09.

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
11. **Nada foi empurrado para o GitHub ainda.** Os commits vivem só na `e0-foundation` local. O `pr.yml` só pode ser verificado depois de um push — decisão do Bruno.
12. **A stack local sobe enxuta.** `supabase start -x "..."` com 12 dos 14 containers excluídos — sobram Postgres e GoTrue, que é tudo que as suítes `db`/`rls` tocam. O GoTrue fica porque `profiles.user_id` e `memberships.user_id` referenciam `auth.users`. Comando completo na seção Commands do `CLAUDE.md`. **A lista precisa de aspas:** o PowerShell interpreta valor separado por vírgula como array e passa só o primeiro nome, silenciosamente.
13. **As policies vão na mesma migration das tabelas**, não numa migration seguinte. Tabela que existe por uma migration que seja com GRANT e sem policy foi legível cross-tenant em algum ponto da história do schema. O ciclo vermelho→verde aconteceu de verdade — a suíte rodou contra as tabelas com GRANT e sem policy e foi vista retornando linhas do tenant errado —, mas a prova mora na mensagem do commit `cc6406a`, não numa migration permanentemente insegura.
14. **Roles `nologin`.** Pool separado exige senha, e senha em migration commitada é segredo vazado; o grant de login fica fora de banda, por ambiente. `grant worker_role, sender_role to postgres` existe para o `postgres` conseguir assumi-los (`SET ROLE`) — não concede nada aos roles.
15. **O primeiro vermelho do E0-07 foi rejeitado.** 11 das 16 falhas eram `permission denied to set role` e `permission denied for table` — isso testa o GRANT, não a policy. Só depois de conceder privilégio de tabela aos três caminhos o vazamento pôde acontecer de fato. Vale a regra geral: **falha por privilégio ausente não é prova de RLS.**

---

## Pendências que continuam abertas

Do plano (§9 e §11), nenhuma resolvida ainda:

- **B-1** VPS de staging · **B-2** conta Logfire · **B-3** conta Grafana Cloud · **B-4** gap-check Meta/lojas/Evolution · **B-5** decidir o ambiente Supabase de staging (recomendação: segundo projeto).
- Telas sem layout mobile; divergências entre o design e `core/telas-da-aplicacao.md`; `core/formulario-perguntas.md` inexistente; LLM do agente indefinido.

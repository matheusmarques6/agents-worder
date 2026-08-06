# Homologacao local com Docker

Esta stack preserva a arquitetura atual do repo:

- `supabase/` continua subindo pelo Supabase CLI local.
- `runtime/` sobe no seu proprio container pelo `docker compose`.
- `hub/` sobe no seu proprio container pelo `docker compose`.

O compose nao cria outro Postgres nem tenta substituir o Supabase local. Os
containers falam com o stack do CLI via variaveis de ambiente.

## Pre-requisitos

- Docker Desktop com `docker compose`
- Supabase CLI instalado na maquina host

## Variaveis de ambiente

1. Crie `.env` na raiz a partir de `.env.example`.
2. Ajuste apenas o que precisar.

Defaults importantes:

- `SUPABASE_DB_URL` aponta para o Postgres local do Supabase CLI em
  `host.docker.internal:54322`.
- `NEXT_PUBLIC_SUPABASE_URL` aponta para a API local do Supabase CLI em
  `host.docker.internal:54321`.
- `AGENTS_RESPONDER` ja vem apontando para o agente real. **Nao existe modo
  inerte para o runtime**: o processo recusa subir sem responder (PR #50),
  porque cair na resposta constante do E1 seria responder a um cliente sem
  passar pelo Judge 1.
- `AGENTS_REVIEWER` ja vem apontando para a auditoria pos-envio (S9b) e e
  **obrigatoria como o responder**: sem ela a fila `q_evals` nao e consumida, o
  "100% avaliado" da janela de shadow vira promessa que ninguem cumpre e
  nenhuma violacao critica e reparada. O processo recusa subir sem ela.
- `AGENTS_CHANNEL` vazio preserva o sender desligado, como o codigo ja faz hoje.

O agente real le a chave do OpenRouter na largada e tambem morre sem ela, entao
o `runtime` so sobe com as duas coisas:

```env
AGENTS_RESPONDER=agents_runtime.agent_core.responder:agent_responder
AGENTS_OPENROUTER_API_KEY=...
```

Se o container ficar reiniciando em laco, e configuracao faltando, nao defeito:
`docker compose logs runtime` mostra qual das duas. Para subir so o hub enquanto
isso nao estiver resolvido, use `docker compose up hub`.

Se quiser ligar o sender da Cloud API, defina tambem:

```env
AGENTS_CHANNEL=agents_runtime.channels.cloud_api:from_env
AGENTS_META_ACCESS_TOKEN=...
AGENTS_META_API_VERSION=v19.0
```

## Subida

1. No Windows com Docker Desktop, materialize o arquivo que o Postgres do
   Supabase monta para o `pgsodium`.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\ensure-supabase-start-secrets.ps1
```

Sem esse arquivo, o bind mount vira diretorio vazio no Linux VM do Docker
Desktop e o container `supabase_db_*` entra em loop com `FATAL: invalid secret
key`.

2. Suba o Supabase local.

Para a homologacao mais completa, incluindo API local e Edge Functions:

```powershell
supabase start
```

Se voce quiser apenas banco local para o `runtime` e nao for usar API local,
Studio nem `ingest-meta`, pode usar o modo enxuto:

```powershell
supabase start -x "realtime,storage-api,imgproxy,kong,mailpit,postgrest,postgres-meta,studio,edge-runtime,logflare,vector,supavisor"
```

3. Aplique as migrations locais:

```powershell
supabase db reset
```

4. Suba `runtime` e `hub`:

```powershell
docker compose up --build
```

5. Abra o hub em `http://localhost:3000`.

## A stack e as suites de teste nao rodam juntas

O container `runtime` consome as mesmas filas do banco local que o nivel
`pipeline` usa. Com ele de pe, um job criado por um teste pode ser reivindicado
pelo container antes do teste chegar nele: o cenario 4a falha com
`predicate never became true: the turn held inside FASE 2`, uma mensagem que
parece defeito do motor e nao e.

Antes de rodar `pytest -m pipeline`, pare o runtime da stack:

```powershell
docker compose stop runtime
```

Devolva depois com `docker compose start runtime`. E a mesma regra que ja vale
entre os niveis `db` e `pipeline`: quem escreve no mesmo banco disputa o mesmo
estado.

## Operacao

Ver logs:

```powershell
docker compose logs -f runtime hub
```

Parar os containers do app:

```powershell
docker compose down
```

Parar o Supabase local:

```powershell
supabase stop
```

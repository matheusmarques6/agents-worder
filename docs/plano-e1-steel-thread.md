# Plano E1 — Steel thread do motor

**Marco:** E1 · **Estimativa:** 7–10 dias úteis · **Pré-requisito de código:** nenhum (o E0 entregou harness, gate e design system)
**Fontes:** `core/arquitetura-plataforma-agentes-whatsapp.md` (ADR-4 a ADR-8), `core/dicionario-de-dados.md` §3–5, `core/testes-e-cicd.md` §3.1 e §3.3, `core/ordem-de-execucao.md` §E1.

---

## 1. O que é o marco

Um **fio de aço**: a mensagem sai do WhatsApp, atravessa o motor inteiro e volta ao WhatsApp — fino, mas de ponta a ponta e com todos os invariantes ligados. O que o E1 entrega não é comportamento de produto, é a **espinha que não pode ser retrofitada**: atomicidade da ingestão, sequências sem corrida, debounce com geração, exclusão mútua por lease + CAS, outbox e envio.

O agente de verdade (LLM, prompt em camadas, tools, Judge) é E2. Aqui a resposta é **fixa**.

**Por que fixa:** um bug de concorrência e um comportamento ruim de modelo produzem o mesmo sintoma — "a resposta saiu errada". Separar os dois é a única forma de depurar qualquer um deles. Enquanto a resposta for constante, toda diferença observada é do motor.

---

## 2. Definição de pronto

O marco fecha quando as cinco provas abaixo estiverem verdes, na ordem em que nasceram vermelhas:

1. **Abandono real na loja dev** → toque chega no WhatsApp de teste, com resposta fixa.
2. **`kill -9` no runtime** no meio do processamento → nada se perde, nada duplica; a fila absorve e a reinicialização conclui.
3. **Heartbeat ≤ 3 min** — o processo publica sinal de vida e o atraso é observável.
4. **Suítes A1–A4 (`db`) e cenários 1–10 (`pipeline`) verdes**, cada uma vista vermelha antes de existir implementação.
5. **Gate de PR verde** nos quatro jobs, com a suíte `rls` cobrindo cada tabela nova.

---

## 3. Fase 0 — decisões que o fio exige (fixadas aqui, antes de qualquer código)

### 3.1 A fatia do schema

O E1 não implementa o dicionário inteiro. Implementa o mínimo que faz o fio existir — e **cada tabela nasce com RLS, policies e a suíte de vazamento**, como no E0-07:

| Tabela | Por que entra no fio | Invariante que ela carrega |
|---|---|---|
| `connector_accounts` §3.2 | resolve o tenant na ingestão de plataforma | `source_account_id` é a chave que resolve o tenant — **nunca o payload** |
| `channels_accounts` §3.1 | resolve o tenant na ingestão de canal e é o destino do envio | `UNIQUE (type, phone_e164)` |
| `contacts` §4.1 | o outro lado da conversa | `UNIQUE (tenant_id, phone_e164)`, E.164 |
| `conversations` §4.2 | onde moram os contadores, a lease e a geração | `next_inbound_seq`, `next_outbound_seq`, `processing_generation`, `processing_token`, `processing_until`, `version`, `last_processed_seq`, `pending_response_at` |
| `messages` §4.3 | o que foi dito, em ordem | **`UNIQUE (conversation_id, direction, seq)`** |
| `webhook_events` §5.1 | idempotência da ingestão | **`UNIQUE (source, source_account_id, external_event_id)`** |
| `message_outbox` §5.3 | nada sai sem passar por aqui | `idempotency_key` UNIQUE; claim só por `claim_outbox_batch()` |

**Fora do E1, deliberadamente:** `orders`, `customers`, `products`, `agent_versions`, `funnels`, `scheduled_touches`, `knowledge_chunks`, as tabelas de avaliação. Nenhuma delas é necessária para o fio, e cada uma custa policies e suíte de vazamento.

**As quatro filas + DLQs.** O E0-08 criou só `q_inbound` e registrou a cobrança: cada fila nova repete a disciplina de grants **com o seu teste**. O E1 cria `q_domain_events`, `q_scheduled`, `q_evals` e os quatro `_dlq`, cada uma com grant a `worker_role` e revoke dos papéis da Data API, e a suíte `db` de exposição cobrindo todas.

**Migrations aditivas.** Nada de editar a 0001/0002 — o E0 podia porque nada estava implantado. A partir daqui vale expand-contract.

### 3.2 O canal da demo

Duas rotas, decididas pelo **B-4** (gap-check da Meta):

- **Rota A — Cloud API com número de teste.** Preferida: é o canal do produto, e a suíte `contract` do E7 vai precisar dele de qualquer forma.
- **Rota B — instância Evolution.** Sobe em minutos, sem aprovação de terceiros, e serve de plano B se o Embedded Signup atrasar.

**Se o B-4 não estiver respondido quando a Fase 2 chegar no sender:** o E1 fecha até o sender **com cassete** — o fio inteiro provado contra um duplo do canal, com a prova 1 da §2 pendente. É a única prova do marco que depende de terceiros, e ela não bloqueia as outras quatro.

### 3.3 Escopo negativo (o que o E1 NÃO faz)

Sem LLM · sem prompt · sem tools · sem Judge · sem funis · sem toques proativos · sem supressão · sem rate limit · sem UI · sem hub. Um PR que traga qualquer um desses volta.

---

## 4. Fase 1 — a especificação executável, vermelha (2–3 dias)

Escrita **antes** da implementação, na ordem abaixo. Cada suíte é um PR; cada PR sobe com a suíte vermelha e o código que a apaga vem no PR seguinte — exceto onde a suíte e a função SQL são a mesma unidade de revisão, e aí o vermelho é registrado no corpo do PR com o log.

### A1 · `ingest_webhook` (`db`) — `core/testes-e-cicd.md` §3.1.1

| # | O que especifica |
|---|---|
| 1 | Erro no meio → **nada** persiste: nem evento, nem mensagem, nem item de fila |
| 2 | Reprocessamento triplo do mesmo webhook → exatamente **1** efeito |
| 3 | Mesmo `external_event_id` em **duas lojas** → 2 eventos distintos |
| 4 | Mesmo `external_event_id` na **mesma loja** → `duplicate`, sem segundo efeito |
| 5 | Tenant resolvido pela **conta de origem**; `tenant_id` no payload é ignorado |
| 6 | Ramo inbound grava `messages` + `seq` + `pending_response_at` e **não enfileira** |
| 7 | Ramo de plataforma enfileira em `q_domain_events` |

### A2 · Contadores de `seq` (`db`) — §3.1.2

| # | O que especifica |
|---|---|
| 1 | Duas conexões concorrentes → sequências **distintas e consecutivas** |
| 2 | Violar `UNIQUE (conversation_id, direction, seq)` é impossível pelo caminho oficial |
| 3 | Nenhum caminho usa `SELECT max(seq)+1` — a trava de fitness do E0-06 já reprova, aqui vira asserção de comportamento |

### A3 · Lease e CAS estendido (`db`) — §3.1.3

| # | O que especifica |
|---|---|
| 1 | Cada condição do CAS falhando **isoladamente** → 0 linhas afetadas: token errado · `version` divergente · `generation` obsoleta · `next_inbound_seq > target_seq` |
| 2 | Caminho feliz → conclusão + `messages` outbound + `message_outbox` **na mesma transação** |
| 3 | A lease só é liberada com o token do dono |
| 4 | Claim numa conversa com lease viva → sem linha retornada |

### A4 · Transação do coalescer (`db`) — §3.1.4

| # | O que especifica |
|---|---|
| 1 | Reversão simulada → `pending_response_at` intacto e **nenhum** job |
| 2 | Sucesso → `processing_generation++` + job em `q_inbound` + campo limpo, **tudo ou nada** |
| 3 | `FOR UPDATE SKIP LOCKED` → dois coalescers não pegam a mesma conversa |

### A5 · `claim_outbox_batch` (`db`) — §3.1.5

Dois consumidores simultâneos → partições disjuntas; respeita `p_limit`; devolve só as linhas atribuídas; não aceita filtro arbitrário; `SECURITY DEFINER` com `search_path` fixo e EXECUTE revogado de PUBLIC.

### Cenários 1–10 (`pipeline`) — §3.3

Os dez, com a adaptação de que "chamada de LLM" vira "chamada do gerador de resposta fixa" — o ponto de cada cenário é o encerramento, não o conteúdo:

| # | Cenário | O que ele prova |
|---|---|---|
| 1 | 5 mensagens em rajada → debounce → **1** job → 1 geração → outbox → sender simulado → `sent` | o caminho feliz inteiro |
| 2 | **Invariante central:** mensagem injetada durante a FASE 2 → CAS falha → rascunho descartado → lease liberada → novo job responde ao conjunto completo | a razão de existir do CAS estendido |
| 3 | Reentrega do pgmq → validações do worker arquivam sem segunda geração (`target_seq <= last_processed_seq`) | dedup é validação, não feature da fila |
| 4 | Kill entre o claim e o send do coalescer → nenhuma conversa órfã, nenhum job duplicado | a transação única do coalescer |
| 5 | Lease expira no meio da FASE 2 → segundo worker assume → CAS do primeiro falha → nada duplicado | lease + CAS juntos |
| 6 | Heartbeat: trabalho longo → VT renovado → sem reentrega durante o trabalho | ADR-4 |
| 7 | Mensagem envenenada → backoff → limite → DLQ da fila certa + alerta; reprocesso manual conclui | pgmq nunca em limbo |
| 8 | Weighted polling 8:4:2:1 sob mistura; evento de domínio > 2 min promovido | ADR-5, com relógio injetado |
| 9 | Semáforo por tenant: tenant no limite tem mensagens devolvidas (`set_vt`); os outros seguem | isolamento entre lojistas |
| 10 | Kill do sender durante o envio → `unknown` → **nenhum reenvio**; status webhook correlacionado → `sent`; janela sem evidência → `manual_review` + alerta | ADR-8, a parte mais fácil de errar |

**Cenários 11–15 ficam fora:** dispatch/funis (E3), takeover (E5), purgas (E6), observabilidade correlacionada (T4/E6) e isolamento de dados de teste — este último **entra já no E1**, porque a suíte `pipeline` cresce aqui e sem prefixo por execução duas rodadas colidem.

### Regras do queueing (`unit`)

Backoff exponencial com jitter · classificação transitório × permanente · weighted polling 8:4:2:1 · promoção por idade (domínio > 2 min, agendado > 10 min) · limites de tentativa por fila (5/5/3/2). **Tudo com o relógio injetado do E0-06** — nenhum teste espera tempo real.

---

## 5. Fase 2 — a implementação apaga os vermelhos (3–5 dias)

Na ordem, um PR por etapa:

1. **Migrations 0003+** — a fatia do §3.1, com policies e suíte `rls` no mesmo PR (decisão 13 do E0: tabela com GRANT e sem policy foi legível cross-tenant em algum ponto da história).
2. **`ingest_webhook()`** — uma transação, resolução de tenant pela conta de origem, os dois ramos.
3. **Filas restantes + grants + teste de exposição** — a cobrança do E0-08.
4. **`queueing` de verdade** — weighted polling, backoff, DLQ, heartbeat, semáforo por tenant.
5. **Coalescer** — tick 2s, transação única, `generation++`.
6. **Lease + CAS** — FASE 1 claim → FASE 2 fora de transação → FASE 3 CAS estendido.
7. **Outbox + sender** — `claim_outbox_batch` `SECURITY DEFINER`, pool do `sender_role`, `unknown` nunca reenviado às cegas.
8. **Adaptador do canal com resposta fixa** — o único ponto que fala com a API externa.
9. **Heartbeat do processo** — sinal de vida observável.

Os invariantes do `CLAUDE.md` são o checklist de revisão de cada PR. Os que mais provavelmente serão violados por engano, em ordem: transação aberta durante trabalho externo · `SELECT max(seq)+1` reintroduzido por conveniência · ingestão enfileirando inbound · algo além do sender chamando a API do canal.

---

## 6. Riscos

| # | Risco | Mitigação |
|---|---|---|
| R1 | **B-4 atrasar** e travar a prova 1 | O fio fecha com cassete; a prova 1 fica pendente e explícita, sem bloquear as outras quatro |
| R2 | Cenários de kill serem instáveis no CI | Encerramento é sinal explícito ao processo, nunca `sleep`; o relógio é injetado; se um cenário oscilar, ele é isolado e investigado, **nunca** marcado como flaky e ignorado |
| R3 | A suíte `pipeline` passar de 5 min e empurrar o gate | `pipeline` já roda só na `main` (E0-11); se crescer demais, paraleliza por arquivo antes de cortar cobertura |
| R4 | O schema do E1 divergir do dicionário por pressa | Cada migration cita a seção do dicionário que a origina; divergência deliberada vira decisão registrada, como na T3 |

---

## 7. O que depende do Bruno

| Item | Bloqueia | Quando |
|---|---|---|
| **B-4** gap-check Meta/Embedded Signup, número de teste, lojas dev, Evolution | a decisão do canal (Fase 0) e a prova 1 (Fase 3) | **agora** — subiu para o caminho crítico |
| **B-5** ambiente Supabase de staging | E0-23 e o primeiro deploy real | antes do E0-23 |
| **B-1/B-2/B-3** VPS, Logfire, Grafana Cloud | T4 → a 8ª prova do E0 | quando existirem |
| Light no Claude Design (seção 12) + frames mobile | E5 / E4 | antes do E4 |

**Já resolvido, sai da lista:** a proteção da `main` foi aplicada no E0-10 (ruleset `main protegida`, decisão 27) e provada com um push direto sendo recusado.

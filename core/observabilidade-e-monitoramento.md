# Observabilidade e Monitoramento — Stack Duplo (Logfire + Grafana Cloud)

**Versão:** 2.0 · **Data:** 2026-08-01 · **Base:** Arquitetura v1.3 · Testes e CI/CD v1.1
**Changelog v2.0:** decisão revisada de plataforma única para **stack duplo** — Pydantic Logfire (depuração e visão LLM) + Grafana Cloud (métricas, dashboards, alerting, IRM/on-call, synthetics). Guia de integração baseado nas documentações oficiais (Logfire e Grafana, consultadas em ago/2026). Healthchecks.io removido — o heartbeat passa a ser do Grafana IRM (um vendor a menos).

---

## 0. Princípios (inalterados) e divisão de responsabilidades

Os quatro princípios da v1.0 permanecem: observabilidade fora da infra observada; dois planos de dados (produto no Postgres × telemetria externa) ligados pelo `trace_id`; PII jamais na telemetria; tudo via OpenTelemetry.

**Quem faz o quê (sem sobreposição):**

| Responsabilidade | Plataforma |
|---|---|
| Depuração de traces, conversas de LLM renderizadas, custo/token por chamada, telemetria do event loop, consulta SQL ad hoc | **Logfire** |
| Métricas (Mimir), logs agregados (Loki), traces para correlação (Tempo), dashboards operacionais | **Grafana Cloud** |
| Alerting (regras sobre métricas/logs) | **Grafana Alerting** |
| On-call, escalonamento, heartbeat/dead man's switch, incidentes | **Grafana IRM** (OnCall faz parte do IRM) |
| Uptime/sondas externas (hub, ingestão, runtime) | **Grafana Synthetic Monitoring** |
| Banco (conexões, disco, queries lentas) | Painel Supabase (nativo) |
| Canal humano final | IRM → webhook → n8n → WhatsApp do Bruno |

Regra de arbitragem: **"o que aconteceu nesta conversa?" → Logfire; "o sistema está saudável e quem acorda se não estiver?" → Grafana.**

---

## 1. Topologia de envio (o coração da integração)

Padrão recomendado pela documentação da Grafana: em produção, a telemetria passa por um Collector — e a distribuição suportada por eles é o **Grafana Alloy** (pipeline robusto com retry, enriquecimento de metadados, redação/amostragem e roteamento para múltiplos backends). Envio direto do SDK é sancionado para dev/teste ou quando não dá para rodar Collector (caso do Vercel).

```
RUNTIME PYTHON (VPS) ── SDK Logfire ──► Logfire (caminho nativo do SDK,
        │                               com o retry próprio do SDK)
        │  cópia OTLP http/protobuf
        ▼
   ALLOY (sidecar docker-compose, localhost:4318)
        │  batch + memory_limiter + redação (2ª linha de defesa de PII)
        ▼
   Grafana Cloud OTLP gateway ──► Mimir (métricas) + Loki (logs) + Tempo (traces)

HUB (Vercel, sem Collector possível) ── OTLP direto ──► Grafana Cloud OTLP gateway
EDGE FUNCTIONS ── logs nativos Supabase + contadores no banco (lidos pelo scraper)
SCHEDULER/JOBS ── ping HTTP ──► endpoints de heartbeat do Grafana IRM
GRAFANA SYNTHETICS ──► sondas contra hub / edge function / runtime /health
GRAFANA ALERTING ──► IRM (escalation chain) ──► webhook ──► n8n ──► WhatsApp
LOGFIRE ALERTS (secundário, SQL sobre spans LLM) ──► webhook ──► n8n ──► WhatsApp
```

**Por que o runtime fala nativo com o Logfire e via Alloy com a Grafana (e não tudo via Alloy):** o SDK do Logfire tem lógica própria de retry e recursos que os exporters OTLP genéricos não têm; e a própria documentação do Logfire define que, ao setar `OTEL_EXPORTER_OTLP_*`, o SDK **adiciona** exporters além do envio ao Logfire — exatamente o comportamento que queremos: uma linha de configuração e as duas plataformas recebem tudo. Se um dia quisermos um cano só, `send_to_logfire=False` + Alloy roteando para os dois é mudança de config, não de código (o Alloy exporta para qualquer endpoint OTLP; o do Logfire aceita OTLP http/protobuf com `Authorization: <write-token>`).

---

## 2. Como integrar — por componente

### 2.1 Runtime Python (VPS)

**Configuração (módulo `obs/` do runtime, carregado antes de tudo no `main.py`):**

```python
import logfire

logfire.configure(
    service_name="agents-runtime",
    environment=os.environ["DEPLOY_ENV"],      # dev | staging | production
    # token via env LOGFIRE_TOKEN
)
# Cópia OTLP para o Alloy local (vai para a Grafana Cloud):
# OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
# OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
# (setar via docker-compose; o SDK cria os exporters adicionais sozinho)

logfire.instrument_httpx()          # Meta, Evolution, lojas, rastreio
logfire.instrument_psycopg()        # ou asyncpg
logfire.instrument_anthropic()      # / instrument_openai() — semântica GenAI c/ tokens e custo
logfire.instrument_system_metrics() # CPU/mem do processo
# FastAPI da API interna: logfire.instrument_fastapi(app)
```

**Boas práticas obrigatórias (viram lint/teste):**
- `service.name` e `deployment.environment` sempre presentes (resource attributes) — sem eles, tudo vira `unknown_service` nos dois backends.
- Atributos padrão em todo span de domínio: `tenant_id`, `conversation_id`, `queue`, `outcome`.
- PII proibida na telemetria (1ª linha: não enviar; 2ª linha: redação no Alloy; a redação automática do Logfire é a 3ª).
- Export é assíncrono e nunca bloqueia o laço; falha de telemetria é log local, jamais erro de negócio.

### 2.2 Alloy (sidecar no docker-compose da VPS)

Arquivo `alloy/config.alloy` (estrutura oficial: receiver OTLP → batch → exporter otlphttp com basic auth de account id + token):

```alloy
otelcol.receiver.otlp "default" {
  http { endpoint = "0.0.0.0:4318" }
  output {
    metrics = [otelcol.processor.batch.default.input]
    logs    = [otelcol.processor.batch.default.input]
    traces  = [otelcol.processor.batch.default.input]
  }
}

otelcol.processor.batch "default" {
  output {
    metrics = [otelcol.exporter.otlphttp.grafana.input]
    logs    = [otelcol.exporter.otlphttp.grafana.input]
    traces  = [otelcol.exporter.otlphttp.grafana.input]
  }
}

otelcol.auth.basic "grafana" {
  username = sys.env("GRAFANA_OTLP_USER")   // account/instance ID
  password = sys.env("GRAFANA_OTLP_TOKEN")  // API token
}

otelcol.exporter.otlphttp "grafana" {
  client {
    endpoint = sys.env("GRAFANA_OTLP_ENDPOINT")  // gateway da região, do portal Grafana Cloud
    auth     = otelcol.auth.basic.grafana.handler
  }
}
```

Complementos de produção: `memory_limiter` antes do batch; processor de redação (atributos `content`, `phone` → drop, cinto extra de PII); fila persistente do exporter (sobrevive a reinício do Alloy). O Alloy expõe as próprias métricas — inclusas no dashboard de operação (observar o observador).

### 2.3 Hub (Next.js/Vercel)

Sem Collector possível → caminho sancionado pela doc da Grafana: **SDK direto ao gateway OTLP**. `instrumentation.ts` com `@vercel/otel`, exportando via env:
`OTEL_EXPORTER_OTLP_ENDPOINT=<gateway>` + `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64(user:token)>` + `OTEL_SERVICE_NAME=agents-hub`.
Uma ação do hub que chama a API interna do runtime propaga `traceparent` via HTTP automaticamente — o trace continua no runtime e aparece nos dois backends. RUM de frontend (Grafana Faro) fica como fase 2, se a depuração de cliente pedir.

### 2.4 Edge Functions (ingestão)

Permanecem minimalistas (latência é sagrada ali): logs estruturados nativos do Supabase + contadores de rejeição gravados em tabela leve, transformados em métrica pelo scraper do runtime. Sem SDK OTel no caminho quente da ingestão no MVP — o trace do evento **nasce** no `traceparent` gravado pelo `ingest_webhook()` (coluna `otel jsonb`), como já definido na Arquitetura §3.2 de traces.

### 2.5 Heartbeats (Grafana IRM — substitui o Healthchecks.io)

Na doc do IRM: cada integração tem **Heartbeat Settings** com um endpoint URL e um intervalo; se o ping esperado não chega, o IRM abre um alert group e dispara a cadeia de escalonamento. Criamos uma integração/heartbeat por processo vital:

| Heartbeat | Quem pinga | Intervalo | Sem ping ⇒ |
|---|---|---|---|
| `runtime-loop` | task do runtime a cada tick | 1 min (janela 3 min) | runtime morto → WhatsApp |
| `coalescer` | ao fim de cada varredura | 1 min (janela 3 min) | debounce parado → WhatsApp |
| `scheduler-daily` | purga TTL ao concluir | 24 h (janela 26 h) | purga não rodou → WhatsApp |
| `reconciler` | reconciliação ao concluir | 15 min (janela 45 min) | poll parado → WhatsApp |

### 2.6 Synthetics, alertas e on-call

- **Synthetic Monitoring** (free tier: 100 mil execuções de teste de API/mês): sondas a cada 1–5 min contra o hub, o endpoint de saúde da Edge Function de ingestão e o `/health` do runtime, de ≥ 2 localizações.
- **Grafana Alerting** implementa a tabela de alertas da v1.0 (§5 — mantida integralmente) como regras sobre Mimir/Loki; severidade vira rótulo; rotas de notificação mandam tudo ao **IRM**, cuja escalation chain única do MVP é: webhook n8n → WhatsApp do Bruno → (sem resposta em 10 min) → ligação/SMS do próprio IRM.
- **Logfire alerts** ficam com o que só ele enxerga bem: veredito `critical` do Judge e anomalia de custo LLM (consultas SQL sobre spans GenAI) → mesmo webhook n8n.

---

## 3. Em que momento do desenvolvimento cada peça entra

Amarrado às fases já planejadas (Plano de Testes A0–A13):

| Fase do desenvolvimento | O que entra de observabilidade | Critério de pronto |
|---|---|---|
| **A0 — infra do repositório** | contas Logfire + Grafana Cloud criadas; Alloy no docker-compose (dev e VPS) com config versionada; envs (`LOGFIRE_TOKEN`, `GRAFANA_OTLP_*`); `logfire.configure()` + instrumentações automáticas no esqueleto do runtime | um span de teste visível **nos dois** backends a partir do ambiente local |
| **Runtime núcleo (com A3/A4)** | atributos padrão de span; `otel jsonb` no payload de fila; extração de `traceparent` no worker; span links no coalescer e no sender; métricas de contador do domínio | trace de um evento sintético navegável de ponta a ponta no Logfire; mesmo trace visível no Tempo |
| **Scheduler/jobs (com A4/A6)** | scraper de métricas (tick 30s → gauges de fila/outbox); 4 heartbeats do IRM configurados e pingando | matar o coalescer em dev → alerta de heartbeat em ≤ 3 min |
| **Hub (com A9)** | `@vercel/otel` → gateway Grafana; `service.name=agents-hub`; erros de servidor com `tenant_id` | ação no hub que chama o runtime aparece como um único trace distribuído |
| **Go-live do 1º tenant (com A13)** | Synthetics (3 sondas); regras dos 6 alertas críticos no Grafana Alerting; escalation chain IRM → n8n → WhatsApp testada de verdade | disparo de alerta sintético chega no WhatsApp em < 2 min |
| **Pré-BF (com A11)** | 3 dashboards completos; alertas de aviso; redação de PII no Alloy validada; **ensaio**: derrubar a VPS em staging e cronometrar heartbeat → WhatsApp | ensaio documentado com tempos; suíte de carga lida nos dashboards |

Regra de ouro do cronograma: **instrumentação nasce com o código (A0), nunca depois** — retrofit de observabilidade é o trabalho mais chato da engenharia; alertas e dashboards podem vir depois, instrumentação não.

---

## 4. Boas práticas consolidadas (das documentações oficiais)

1. **Collector em produção, SDK direto só onde Collector não roda** (Grafana): Alloy dá batch, retry, redação e roteamento múltiplo — o app não deve conhecer N backends.
2. **HTTP, não gRPC, nos dois gateways**: o endpoint do Logfire aceita OTLP apenas via HTTP (protobuf/JSON); o exporter recomendado para o gateway da Grafana Cloud é `otelcol.exporter.otlphttp`. gRPC contra esses endpoints falha de formas confusas.
3. **Autenticação**: Logfire = header `Authorization: <write-token>`; Grafana Cloud = basic auth (instance ID + API token). Tokens só em env/secret manager — nunca no config versionado (`sys.env()` no Alloy).
4. **Resource attributes disciplinados**: `service.name`, `deployment.environment`, `service.version` (= SHA do deploy) em tudo — é o que faz os dados dos dois backends serem correlacionáveis entre si e com releases.
5. **Cardinalidade de métricas sob controle**: `tenant_id` como rótulo é aceitável com 25 tenants; `conversation_id`/`contact_id` como rótulo de métrica **nunca** (explode séries no Mimir) — esses ficam em atributos de span/log.
6. **Heartbeat para tudo que falha em silêncio** (IRM): a ausência de sinal é o sinal — scheduler, coalescer, purga, reconciliador.
7. **Amostragem**: 100% no MVP (volume ~2M spans/mês cabe nos free tiers de ambos); quando apertar, amostrar caminho feliz e manter 100% de erros/DLQ/CAS-fail — decisão aplicada no Alloy (Grafana) e no SDK (Logfire), não no código.
8. **Observar o observador**: métricas do próprio Alloy no dashboard; se o exporter para a Grafana começar a falhar/enfileirar, isso alerta — senão o stack duplo degrada para simples em silêncio.
9. **Um runbook por alerta crítico**: todo alerta da tabela aponta para uma seção de runbook (o que olhar, em qual plataforma, qual ação) — alerta sem runbook vira ruído.

---

## 5. Custos e gatilhos

- **MVP: R$ 0/mês** — free tiers: Logfire (10M spans/mês), Grafana Cloud (tier gratuito com métricas/logs/traces/IRM/Synthetics — 100k execuções de API synthetics/mês).
- **Primeiro custo esperado:** Logfire Team (US$ 49/mês) por retenção/volume, ou Grafana Cloud pago se as séries de métricas/logs passarem do free tier na BF.
- **Gatilhos de revisão:** volume > 70% de qualquer free tier → amostragem (prática 7) antes de pagar; time contratado → ativar rotação/escala real no IRM (a fundação já está pronta); cliente enterprise exigindo dados em casa → migrar o destino do Alloy para LGTM self-hosted (mesmo código, mesmo config model — a rota de saída que motivou a escolha da Grafana).

## 6. Registro da decisão (resumo ADR-Obs-2, substitui ADR-Obs-1)

- **Decisão:** stack duplo Logfire + Grafana Cloud; runtime fala nativo com Logfire e envia cópia OTLP ao Alloy local, que entrega ao gateway da Grafana Cloud; hub direto ao gateway; heartbeats e on-call no Grafana IRM; synthetics na Grafana; alertas críticos por IRM → n8n → WhatsApp; Logfire alerts para sinais exclusivamente LLM.
- **Consequências:** melhor ferramenta de cada categoria sem duplicar função; um sidecar a operar (Alloy); portabilidade dupla garantida (OTel + rota self-host LGTM); custo zero no MVP.
- **Riscos aceitos:** dois vendors para manter contas/tokens; disciplina de "quem faz o quê" precisa ser respeitada para os dois painéis não divergirem (mitigado pela regra de arbitragem do §0 e pela prática 8).

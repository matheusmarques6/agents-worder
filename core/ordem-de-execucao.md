# Ordem de Execução v2.0 — Orientada a Testes e a Design

**Versão:** 2.0 · **Data:** 2026-08-01 · **Substitui:** v1.1
**Mudança de premissa:** o Bruno definiu as duas orientações do desenvolvimento — **testes** (test-first) e **design** (design-first) — e **todas as telas já estão prontas** (design finalizado). A ordem foi reorganizada para que essas duas forças dirijam cada dia de trabalho, não apenas o resultado final.

---

## 0. As duas orientações como regras operacionais

### R1 — Orientação a testes (test-first em todos os níveis)
Nenhuma linha de código de produção nasce sem um teste vermelho que a exija. O Plano de Testes v1.0 e o mapa Testes/CI-CD v1.1 deixam de ser "verificação depois" e viram **especificação antes**:
- **Invariantes do motor** (ingestão atômica, CAS estendido, coalescer, outbox, filas): o teste de integração do cenário nasce primeiro (vermelho), a implementação vem para deixá-lo verde, e só então refatora. Os 15 cenários da suíte de pipeline e os casos das suítes DB/RLS **são** a especificação executável.
- **Regras de negócio** (supressão, rate limits, anti-ban, staleness): unitário primeiro, sempre.
- **Interface**: a jornada E2E (Playwright) da tela é escrita **a partir do documento de Telas v1.1 antes da tela existir** (ATDD) — a tela está pronta quando a jornada passa.
- Ritmo diário: **vermelho → verde → refatorar → commit** (CI bloqueante a cada push, como já definido).

### R2 — Orientação a design (as telas prontas são contrato)
- O design finalizado é a fonte da verdade visual. Nenhuma tela é "interpretada" na hora de codar; divergência do design é bug ou mudança de escopo explícita (atualiza o design e o doc de Telas primeiro).
- **Design system antes da primeira tela:** tokens (cores, tipografia, espaçamento) e componentes reutilizáveis são extraídos do design **uma vez** e viram a biblioteca que todas as telas consomem — é o que garante fidelidade e velocidade nas ~50 telas.
- **Fidelidade é testada, não conferida no olho:** regressão visual no Playwright — captura da tela implementada comparada à linha de base exportada do design, em viewport desktop **e** mobile (a diretriz "ambos com o mesmo capricho" vira asserção).
- Se as telas estão no Figma, o Claude Code consome os frames diretamente (MCP do Figma) para gerar componentes fiéis — tokens e medidas saem do arquivo, não de estimativa.

### Definição de pronto universal (toda entrega, motor ou tela)
1. Teste escrito antes e agora verde (no nível mais baixo que prova a entrega);
2. Tela fiel ao design (regressão visual verde nos dois viewports) — quando houver tela;
3. CI completo verde; 4. Observabilidade da entrega no ar (spans/métricas do que nasceu);
5. Nenhum S1/S2 aberto criado pela entrega.

---

## 1. Estrutura dual-track (para um dev com Claude Code)

Duas trilhas com naturezas diferentes, intercaladas — a espinha continua sendo o motor (dependência técnica), mas com as telas prontas a trilha de interface começa **muito** mais cedo que na v1.1:

- **Trilha Motor (test-first):** E1 → E2 → E3 — a sequência de risco técnico não muda; o que muda é o método (teste antes, sempre).
- **Trilha Interface (design-first):** começa já no E0 com o design system e avança em blocos (formulário → hub → admin) assim que o backend de cada bloco existe. Com Claude Code, a alternância motor/interface pode ser diária ou por agentes em paralelo — a regra é só uma: **interface nunca espera "sobrar tempo"; ela tem blocos nomeados no cronograma.**

```
E0 Fundação + Design System ─┬─ E1 Steel thread ─ E2 Agente ─ E3 Recuperação ─┐
   (tokens+componentes+       │   (motor, TDD)     (TDD+evals)  (TDD)         │
    harness de regressão      └────────────── UI estática do formulário       │
    visual prontos)                           pode nascer aqui (A1–A2.7) ─────┤
                                                                              ▼
                 E4 Onboarding (ligar UI ao motor + gates) → E5 Hub → E6 Admin+Obs
                                                                              ▼
                                     E7 Endurecimento + Piloto (shadow = UAT) → E8 Conectores
```

---

## 2. Marcos revisados

### E0 — Fundação + Design System (6–9 dias)
**Objetivo:** tudo do antigo M0 **mais** a fundação de design que as duas orientações exigem.
**Entra (test):** harness de teste completo desde o dia 1 — pytest com os marcadores, Postgres efêmero no CI, Playwright configurado com **regressão visual** e os dois viewports padrão; os gates bloqueantes do PR ativos antes da primeira feature.
**Entra (design):** extração do design system das telas prontas — tokens, tipografia, componentes base (botões, campos, cards, tabelas, balão de conversa, wizard) — com uma página-vitrine dos componentes; linhas de base visuais exportadas do design para o harness.
**Entra (infra):** monorepo, projetos Supabase, VPS staging, Alloy, Logfire+Grafana com span de teste nos dois, e o **disparo dos cadastros externos** (verificação Meta incluindo Embedded Signup, lojas dev nas 3 plataformas, números de teste, Evolution).
**Prova de conclusão:** CI verde com um teste de cada nível rodando (mesmo triviais); um componente do design system renderizado passando na regressão visual contra a linha de base do design, em desktop e mobile.

### E1 — Steel thread do motor, test-first (7–10 dias)
Igual ao antigo M1 no conteúdo (fio: webhook → `ingest_webhook` → filas → coalescer → lease/CAS → outbox → sender → WhatsApp real, resposta fixa), com o método explícito: **as suítes A1, A2 e os cenários 1–10 da pipeline são escritos primeiro e ficam vermelhos**; a implementação existe para apagá-los um a um. 
**Prova de conclusão:** a demo do abandono na loja dev chegando no WhatsApp de teste **+** o quadro de testes mostrando os cenários todos verdes na ordem em que nasceram vermelhos; matar o runtime no meio → nada se perde; heartbeat no WhatsApp em ≤ 3 min.

### E2 — Agente real, test-first + eval-first (10–14 dias)
Conteúdo do antigo M2 (prompt em camadas, tools, Judge 1, contexto, pgvector) com uma inversão importante: **as rubricas do Judge e o pack base de cenários nascem antes do agente** — o eval harness é o "teste vermelho" da qualidade. O agente é desenvolvido até o pack passar do limite mínimo, não até "parecer bom".
**Prova de conclusão:** conversa real de suporte no número de teste; pack base ≥ limite; custo/latência no Logfire; cenário 14 de observabilidade verde.

### E3 — Recuperação completa, test-first (7–10 dias)
Antigo M3 (funis, supressão, rate limits, staleness, `order_paid`, reconciliação, Evolution + anti-ban, tier Cloud) — cada regra de proteção nasce como unitário vermelho; os fluxos, como cenários de pipeline vermelhos.
**Prova de conclusão:** simulação do dia real (funil → resposta mata toque → conversão; pagamento cancela; bloqueio suprime) com a suíte A5+A6 verde e primeira carga leve (10x).

### Bloco paralelo E1–E3 — UI estática do formulário (3–4 dias, intercalados)
Com design system pronto e telas prontas, as telas A1–A2.7 (wizard) **não dependem do motor** — nascem como UI com estado local + regressão visual + testes de componente das validações ("à prova de resposta ruim"). Quando E4 chegar, o trabalho é ligar, não construir. Este bloco também é a válvula de variedade: dias de fadiga do motor viram dias de interface **planejados**, não fuga.

### E4 — Onboarding self-service completo (8–10 dias — reduzido: UI já existe)
Ligar o wizard ao backend: salvamento por etapa, OAuth, **Embedded Signup**, QR Evolution + aceite, agente gerador (formulário→`agent_versions` draft), gates C3 (admin) e B4 (cliente) com chat simulado e cenários, e-mail/senha, shadow automático.
**Método:** jornadas E2E do onboarding escritas primeiro a partir do doc de Telas; regressão visual em tudo.
**Prova de conclusão:** onboarding inteiro sem tocar no banco, cronometrado (< 1 dia de calendário), E2E verde nos dois viewports.

### E5 — Hub operacional (9–12 dias)
Antigo M5 (dashboard com receita recuperada, inbox realtime + takeover, versões/rollback, testes, catálogo, funis, configurações/equipe). Cada tela: jornada E2E vermelha → componentes do design system → verde → regressão visual.
**Prova de conclusão:** as 10 jornadas E2E do hub verdes em desktop e mobile; `attendant` bloqueado do que é de `owner`; acessibilidade básica (axe) verde.

### E6 — Admin completo + observabilidade total (5–7 dias)
Antigo M6 (gate, shadow, alertas, DLQ/outbox, custo/judges, flywheel, cenários, purga LGPD, auditoria; dashboards Grafana, Synthetics, escalation chain testada).
**Prova de conclusão:** cada tipo de falha simulada em staging → alerta certo no canal certo; jornadas admin E2E verdes.

### E7 — Endurecimento + piloto real (7–10 dias + 7 de calendário de shadow)
Igual ao antigo M7: contrato real (incl. duplicidade da Cloud API), carga com critérios quantitativos, os dois exercícios de restauração, purgas, revisão de segurança; 1º tenant real da operação manual em **shadow de 7 dias = UAT**.
**Prova de conclusão = "aplicação rodando e funcional":** 1º tenant real operando pós-shadow, zero S1/S2, gate de release verde, alertas silenciosos por 72h.

### E8 — Conectores restantes (4–6 dias)
Nuvemshop e Yampi sobre a porta provada; suíte de contrato dos dois; 2º e 3º tenants reais.

---

## 3. O ciclo padrão de cada entrega (o "como" de todo dia)

```
1. Pegar o próximo item (invariante do motor OU tela do inventário)
2. Escrever o teste no nível mais baixo que o prova → VERMELHO
   (motor: unit/db/pipeline · tela: jornada E2E + linha de base visual)
3. Implementar o mínimo que apaga o vermelho
   (tela: só componentes do design system; nada de CSS avulso)
4. VERDE → refatorar com os testes segurando
5. Tela? → regressão visual desktop+mobile contra o design
6. Commit → CI bloqueante → deploy automático em staging
7. Item pronto pela Definição de Pronto (§0) — próximo item
```

## 4. O que muda em relação à v1.1 (resumo honesto)

| Aspecto | v1.1 | v2.0 |
|---|---|---|
| Papel dos testes | Escritos junto/logo após, suítes bloqueantes | **Escritos antes; são a especificação** — inclusive as rubricas de eval antes do agente |
| Papel do design | Doc de telas como escopo | **Design pronto como contrato**, design system extraído no E0, fidelidade testada por regressão visual |
| Início da interface | M4 (semana ~6) | **E0** (design system) e bloco estático do formulário durante E1–E3 |
| Estimativas | 64–88 dias úteis | **66–92 dias úteis** (soma do E0 maior e do bloco paralelo; E4 encolhe porque a UI já existe) — o custo do test-first é pago em dias e devolvido em retrabalho que não acontece |
| Risco novo | — | Fadiga de TDD → mitigada pela regra pragmática: test-first vale para **invariantes, regras e jornadas**; detalhe visual é coberto por regressão visual, não por unitário de componente |

## 5. Riscos de sequenciamento (mantidos + novos)

1. Verificação Meta / Embedded Signup atrasar → cadastros no E0, dia 1.
2. Semanas "invisíveis" do motor → demos no WhatsApp por marco **+** agora o bloco paralelo de UI dá progresso visível também na tela.
3. Pular o texto fixo do E1 → resistir (isola concorrência de comportamento de modelo).
4. **Novo — linha de base visual instável** (fontes/rendering variando entre máquinas): regressão visual roda **só no CI** (ambiente fixo), nunca comparando capturas locais.
5. **Novo — design mudar durante o build**: mudança de design passa por atualizar linha de base + doc de Telas no mesmo PR — a regressão visual transforma mudança silenciosa em diff explícito.

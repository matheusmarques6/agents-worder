# Telas da Aplicação — Inventário Completo

**Versão:** 1.1 · **Data:** 2026-08-01 · **Base:** Requisitos v1.2 · Arquitetura v1.3 · Dicionário de Dados v1.0
Toda tela referencia os requisitos que cobre. Estados (vazio, carregando, erro) estão anotados por tela — não são telas separadas. As três premissas da v1.0 foram **confirmadas pelo Bruno** (§5).

**Diretriz de plataforma (confirmada): desktop e mobile com o mesmo capricho desde o MVP.** Toda tela do hub e do formulário tem layout mobile de primeira classe — em especial o wizard (lojista preenche no celular), o inbox (atendimento acontece no celular) e o dashboard.

**Áreas:** A = Formulário público (sem conta) · B = Hub do lojista · C = Admin (plataforma) · D = Transversais.

---

## 1. Área A — Formulário público de onboarding (sem conta)

Acessível só por link de convite (`/f/{token}`). Wizard com progresso salvo a cada etapa (o cliente pode fechar e voltar pelo mesmo link). À prova de resposta ruim: exemplos, opções pré-prontas, validação inline (RF-002).

| ID | Tela | Conteúdo e ações | Estados / observações | RF |
|---|---|---|---|---|
| A1 | Boas-vindas do convite | Nome da loja (pré-preenchido pelo admin), o que vai acontecer, tempo estimado (~15 min), botão iniciar | Token inválido/expirado → tela de convite inválido com contato | RF-001 |
| A2.1 | Etapa — Identidade do agente | Nome do agente, tom (3 presets com exemplo de mensagem), nível de emoji (3), lista de palavras proibidas, frases de abertura | Prévia ao vivo de uma mensagem de exemplo conforme escolhe | RF-002 |
| A2.2 | Etapa — Objetivo e comportamento | Objetivo primário, follow-up proativo (on/off com explicação), "nunca dizer que é IA" (on/off, default on) | | RF-002, RF-014, RF-018 |
| A2.3 | Etapa — Atendimento (intents e-commerce) | Módulos que o agente cobre: rastreio, status de pedido, trocas/devoluções, dúvidas de produto, FAQ/políticas — com campos de política por módulo (ex.: prazo de troca) | Cada módulo ligado habilita a tool correspondente | RF-002, RF-017 |
| A2.4 | Etapa — Preço e apresentação de produto | Política de preço (4 opções), formato de apresentação (link/foto/texto), máximo de links, fallback sem estoque | | RF-002 |
| A2.5 | Etapa — Escalonamento | Quando chamar humano (gatilhos), canal de escalonamento, dados enviados no payload | | RF-002, RF-019 |
| A2.6 | Etapa — Base de conhecimento | Colar FAQ/políticas (texto) e/ou upload de arquivo; lista do que já foi adicionado | Vazio permitido com aviso do impacto | RF-002 |
| A2.7 | Etapa — Catálogo | Fonte: plataforma (sync automático) / upload CSV-XLSX / Google Sheets (link) | Upload mostra prévia e erros de coluna | RF-002, RF-042 |
| A3 | Conexão da loja | Escolha da plataforma (Shopify / Nuvemshop / Yampi) → OAuth na janela da plataforma → retorno com sucesso | Erro de OAuth → refazer; sucesso mostra nome da loja conectada | RF-003 |
| A4a | Conexão WhatsApp — oficial (Cloud) | **Embedded Signup da Meta embutido no fluxo** (confirmado): pop-up oficial da Meta, cliente conecta o próprio número; a plataforma recebe e guarda WABA ID, phone number ID e token no Vault. Se a conexão for do admin (definido no convite), a tela vira informativa: "seu número será conectado pela nossa equipe" | Estados conectando/erro/sucesso com número exibido; erro do pop-up com instrução de retry | RF-003 |
| A4b | Conexão WhatsApp — não-oficial (Evolution) | QR code + verificação de status em tempo real até parear | QR expirado → gerar novo | RF-003 |
| A4c | Aceite de risco (Evolution) | Texto claro do risco de banimento + mecanismos anti-ban + checkbox de aceite obrigatório | Aceite registrado em auditoria com data/hora | RF-004, RNF-044 |
| A5 | Revisão e envio | Resumo de todas as respostas com editar por etapa; botão concluir | Concluir dispara o agente gerador (RF-005) | RF-002, RF-005 |
| A6 | Informar e-mail | Campo de e-mail para criação da conta + explicação do que vem a seguir (testes e aprovação) | | RF-007 |
| A7 | Confirmação | "Verifique seu e-mail para criar a senha"; resumo do status: agente criado e em aprovação | Reenviar e-mail | RF-007 |
| A8 | Criar senha | Via link do e-mail; define senha → entra no hub direto | Link expirado → reenviar | RF-007 |

## 2. Área B — Hub do lojista

Autenticado (Supabase Auth). Toda tela respeita permissões do papel (`owner | manager | attendant`) — RF-047. Interface em PT-BR; **desktop e mobile com o mesmo capricho** (confirmado).

| ID | Tela | Conteúdo e ações | Estados / observações | RF |
|---|---|---|---|---|
| B1 | Login | E-mail + senha; link esqueci a senha | | RF-007 |
| B2 | Recuperar senha | Fluxo padrão por e-mail | | — |
| B3 | Dashboard (home) | Métricas do período: conversas, atendimentos da IA vs humano, recuperações iniciadas/convertidas e **receita recuperada em R$** (confirmado) — regra de atribuição: pedido pago pelo mesmo contato dentro da janela de atribuição após um toque de funil (padrão proposto: 24h, configurável por tenant); últimos pedidos; status do agente | Tenant em onboarding: banner "agente em aprovação" com link para B4; tenant em shadow: selo shadow | RF-041 |
| B4 | Aprovação do agente (gate do cliente) | Prompt formatado legível, tools ativas, cenários executados com resultado e score, chat simulado embutido, campo "apontar ajustes" (vai para o admin), botão aprovar e ativar | Só aparece após aprovação do admin; aprovação registra auditoria e liga o shadow | RF-006, RF-008 |
| B5a | Inbox — lista de conversas | Tempo real; filtros: ativas, com humano, por ocasião (PIX/checkout/carrinho/direto), busca por contato | Vazio: "nenhuma conversa ainda" | RF-040 |
| B5b | Inbox — conversa | Mensagens ao vivo (Realtime), painel lateral com contexto (pedidos, slots), botão **Assumir conversa** (takeover) e **Devolver para IA**; indicação de quem assumiu; composer habilitado só em takeover | Agente em observação durante takeover (aviso sutil) | RF-016, RF-040 |
| B6a | Agente — visão geral | Status (ativo/pausado/shadow), pausar/despausar, número conectado e tipo, tools ativas, prompt formatado (leitura) | Pausar pede confirmação | RF-043, RF-045 |
| B6b | Agente — editar personalidade | Formulário estruturado (mesmos campos da A2.1–A2.5) + edição avançada de texto; salvar cria **nova versão** que passa a valer | Aviso: "isso muda o agente em produção"; diff antes de salvar | RF-044 |
| B6c | Agente — versões | Lista de versões (autor, origem, data, resumo), diff com a anterior, botão restaurar (rollback em 1 clique) | | RF-044 |
| B7a | Testes — chat simulado | Conversa com o próprio agente em ambiente de teste (não envia WhatsApp), com contexto simulado selecionável (ex.: cliente com pedido X) | | RF-045 |
| B7b | Testes — cenários | Rodar pack de cenários; tabela de resultados com score por cenário; detalhe da conversa sintética | Execução assíncrona com progresso | RF-045 |
| B7c | Testes sazonais | Fila de conversas reais selecionadas para o lojista aprovar/desaprovar com comentário | Vazio quando não há seleção pendente | RF-046 |
| B8a | Catálogo — produtos | Tabela (busca, preço, estoque, ativo), origem da fonte, última sincronização | Fonte plataforma: somente leitura | RF-042 |
| B8b | Catálogo — upload CSV/XLSX | Upload → mapeamento de colunas → prévia → confirmar; relatório de erros por linha | | RF-042 |
| B8c | Catálogo — Google Sheets | Colar link, testar acesso, status da sincronização viva | Erro de permissão do Sheets com instrução | RF-042 |
| B9 | Recuperação — funis | Cards por ocasião (PIX, checkout, carrinho): on/off, cadência resumida, editar copy base por toque, follow-up proativo on/off | Mostra contagem de toques/conversões do período | RF-031, RF-018 |
| B10a | Configurações — geral | Idioma principal do agente, "nunca dizer que é IA", dados da loja | | RF-013, RF-014 |
| B10b | Configurações — equipe | Lista de membros, convidar por e-mail, papel e permissões, remover | Convite por e-mail (transacional D2) | RF-047 |
| B10c | Configurações — canais | Números conectados, tipo, status, tier Meta (se Cloud) com barra de uso, uso diário (se Evolution) | Ação de reconectar quando cair | RF-035, RF-036 |

## 3. Área C — Admin (operador da plataforma)

Mesmo app, rota `/admin`, acesso só `is_platform_admin`. Cliente jamais vê judges, custos ou filas (RF-050).

| ID | Tela | Conteúdo e ações | Estados / observações | RF |
|---|---|---|---|---|
| C1 | Visão geral da plataforma | Tenants por status, alertas abertos por severidade, resumo das filas (profundidade/idade), custo LLM do dia | Primeira tela após login admin | RF-050, RF-052 |
| C2a | Tenants — lista | Tabela com status (onboarding/shadow/ativo/pausado/cancelado), busca, criar cliente | | RF-001 |
| C2b | Criar cliente + convite | Nome, negócio, **quem conecta o número oficial (eu/cliente)** → gera link do formulário para copiar/enviar | Reemitir convite; expirar convite | RF-001 |
| C2c | Tenant — detalhe | Abas: visão geral (status, ações pausar/ativar/cancelar), agente e versões, conversas, catálogo, logs do tenant, configurações | Cancelar inicia contagem dos 10 dias de purga com aviso | RF-051, RF-072 |
| C3 | Gate do admin — revisão do agente | Prompt gerado, respostas do formulário de origem, cenários executados com scores, chat simulado, ajustar prompt (gera versão), **aprovar** (libera o gate do cliente) | Reprovar devolve para ajuste com anotações | RF-006 |
| C4 | Shadow — fila de acompanhamento | Conversas dos tenants em shadow, 100% avaliadas, ordenadas por pior score; marcar como visto; abrir conversa; criar patch de prompt a partir dela | Contador de dias restantes de shadow por tenant | RF-008 |
| C5 | Central de alertas | Lista por severidade/tipo/tenant, reconhecer/resolver, link para o trace (Logfire) e para o objeto (conversa, fila, número) | Espelha a tabela `alerts`; mesmo conteúdo que chega no WhatsApp | RF-052 |
| C6a | Filas — DLQs | Por fila: mensagens com erro, payload, classe do erro, tentativas; **reprocessar em 1 clique**; descartar com motivo | Vazio é o estado desejado (destacar) | RF-053 |
| C6b | Outbox — revisão manual | Itens `unknown`/`manual_review`: conteúdo, contato, histórico de tentativas, evidências (status webhook); decidir **reenviar** ou **descartar** | Ações registram auditoria | RF-053 |
| C7a | Observabilidade — custo LLM | Custo por tenant/dia/modelo/finalidade, tendência, anomalias | Fonte: `llm_calls` (verdade de produto) | RF-050 |
| C7b | Observabilidade — judges | Distribuição de scores por tenant, vereditos críticos com link para conversa e trace | | RF-050 |
| C7c | Observabilidade — tools/latência | Tool calls por tipo, taxa de erro, latências | | RF-050 |
| C8 | Flywheel — patches propostos | Lista de patches (origem, evidência/score que motivou, diff do prompt), aprovar → cria versão para o tenant / rejeitar | | RF-062 |
| C9 | Cenários — packs base | CRUD dos packs por ocasião; gerar variações por tenant (IA) e revisar antes de salvar | | RF-060 |
| C10 | LGPD — purga por contato | Busca por telefone → mostra o que existe (nº de conversas, contextos, embeddings) → confirmação dupla → executa e mostra resultado | Ação irreversível, auditada | RF-054 |
| C11 | Auditoria | `audit_log` com filtros (tenant, ator, ação, período) | | RNF-052 |

## 4. Área D — Transversais

| ID | Item | Conteúdo |
|---|---|---|
| D1 | Erros | 404, sem permissão (403 com explicação do papel), erro genérico com id de correlação (trace_id) para suporte |
| D2 | E-mails transacionais | (1) Criar senha (pós-formulário); (2) Convite de membro da equipe; (3) Recuperar senha |
| D3 | Estados globais | Carregando (skeleton), vazio com orientação, offline/reconectando no inbox realtime |

**Contagem:** 14 telas na área A (com 7 etapas do wizard), 18 no hub, 15 no admin, 3 transversais — **~50 telas/estados nomeados**, todas rastreáveis a RF.

---

## 5. Premissas confirmadas (decisões do Bruno, 2026-08-01)

1. **Conexão Cloud API pelo cliente = Embedded Signup da Meta embutido no formulário** (A4a atualizada). Implicações: `channels_accounts` guarda WABA ID + phone number ID; o token vai ao Vault; pré-requisito externo: app Meta aprovado para Embedded Signup — já coberto pelos cadastros do M0 da Ordem de Execução.
2. **Receita recuperada em R$ entra no MVP, com janela de atribuição** (B3 atualizada). Regra: pedido pago pelo mesmo contato dentro da janela após um toque conta como recuperado; janela padrão proposta de **24h**, configurável por tenant (`tenants.attribution_window_hours`) — ajustar o padrão com dados reais do piloto.
3. **Desktop e mobile com o mesmo capricho desde o MVP** (diretriz global no topo). Implicação de esforço refletida na Ordem de Execução (M4/M5).

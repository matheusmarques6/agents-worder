-- E1 · A1 — as três filas que faltavam e os quatro DLQs.
--
-- O E0-08 criou só `q_inbound` e deixou a cobrança registrada: cada fila nova
-- repete a disciplina de grants **com o seu teste**. Isto aqui é essa
-- replicação, não um padrão novo.
--
-- Os grants do E0-08 valeram apenas para os objetos que existiam quando
-- rodaram: `grant ... on all sequences in schema pgmq` não alcança sequência
-- criada depois. Por isso cada fila nova regrant explicitamente — é o tipo de
-- detalhe que só aparece quando o worker tenta enfileirar em produção.

select pgmq.create('q_domain_events');
select pgmq.create('q_scheduled');
select pgmq.create('q_evals');

-- Um DLQ por fila. Toda leitura termina em archive, set_vt com backoff ou
-- aqui — pgmq nunca fica em limbo (ADR-6).
select pgmq.create('q_inbound_dlq');
select pgmq.create('q_domain_events_dlq');
select pgmq.create('q_scheduled_dlq');
select pgmq.create('q_evals_dlq');

comment on table pgmq.q_q_domain_events is
    'Eventos de plataforma e canal: abandono, pagamento, status. Payload {webhook_event_id}.';
comment on table pgmq.q_q_scheduled is
    'Toques vencidos. Payload {scheduled_touch_id}.';
comment on table pgmq.q_q_evals is
    'Avaliação, melhor esforço. Payload {kind, conversation_id?, eval_run_id?}.';

-- ---------------------------------------------------------------------------
-- Quem pode dirigir as filas
-- ---------------------------------------------------------------------------
-- Mesma leitura do E0-08: as funções do pgmq rodam como quem chama, então ler
-- um job exige privilégio de tabela e não só EXECUTE. `sender_role` continua
-- ausente de propósito — senders drenam a outbox, não consomem job.
grant select, insert, update, delete on
    pgmq.q_q_domain_events,
    pgmq.q_q_scheduled,
    pgmq.q_q_evals,
    pgmq.q_q_inbound_dlq,
    pgmq.q_q_domain_events_dlq,
    pgmq.q_q_scheduled_dlq,
    pgmq.q_q_evals_dlq
    to worker_role;

grant select, insert on
    pgmq.a_q_domain_events,
    pgmq.a_q_scheduled,
    pgmq.a_q_evals,
    pgmq.a_q_inbound_dlq,
    pgmq.a_q_domain_events_dlq,
    pgmq.a_q_scheduled_dlq,
    pgmq.a_q_evals_dlq
    to worker_role;

-- Alcança as sequências recém-criadas — as do E0-08 já estavam concedidas, mas
-- aquele grant não vale para o que nasceu depois.
grant usage, select on all sequences in schema pgmq to worker_role;

-- ---------------------------------------------------------------------------
-- E quem não pode
-- ---------------------------------------------------------------------------
revoke all on all tables in schema pgmq from anon, authenticated, service_role;

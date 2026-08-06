-- S9b — post-hoc evaluation: who enqueues it, and how a correction gets out.
--
-- Two additive changes, no destructive step, N-1 compatible: a runtime from
-- before this migration keeps working against the expanded schema, because
-- `conclude_turn` keeps its signature and its return type, and the new function
-- is simply unused until a consumer exists.
--
-- ## Why `conclude_turn` is the one that enqueues (decisão 91)
--
-- The evaluation job is created HERE, in the same transaction that inserts the
-- outbox row, and nowhere else. The two alternatives are worse in ways that
-- reach the customer:
--
--   * the responder cannot: it runs in FASE 2, BEFORE the extended CAS. A draft
--     killed by a newer message would be evaluated despite never being sent —
--     and a `critical` verdict on it would send the customer a correction to a
--     message they never received;
--   * the worker after the commit cannot: it would touch the engine (Lei 1 of
--     the E2 plan) and open a window where the process dies between commit and
--     enqueue. Inside the shadow window, "100% evaluated" would become a
--     silent lie.
--
-- Atomic here means: either the message and its evaluation job both exist, or
-- neither does.
--
-- The payload carries ids only — the same rule as `apply_domain_event`
-- (decisão 74). Everything else lives on the row, and a fatter job is just a
-- copy that can drift from the truth.

create or replace function internal.conclude_turn(
    p_conversation_id  uuid,
    p_token            uuid,
    p_expected_version integer,
    p_generation       integer,
    p_target_seq       integer,
    p_content          jsonb,
    p_idempotency_key  text,
    p_kind             text default 'reply'
)
    returns internal.turn_outcome
    language plpgsql
    set search_path = pg_catalog, public, internal
as $$
declare
    v_tenant_id  uuid;
    v_contact_id uuid;
    v_channel_id uuid;
    v_channel    text;
    v_seq        integer;
    v_outbox_id  uuid;
    v_message_id uuid;
begin
    update public.conversations
       set last_processed_seq = p_target_seq,
           processing_token = null,
           processing_until = null,
           version = version + 1
     where id = p_conversation_id
       and processing_token = p_token
       and version = p_expected_version
       and processing_generation = p_generation
       and next_inbound_seq = p_target_seq
    returning tenant_id, contact_id, channel_account_id
      into v_tenant_id, v_contact_id, v_channel_id;

    if not found then
        -- O rascunho morre aqui, e morrer é o comportamento correto. A lease
        -- fica para quem for o dono dela; quem chamou usa `release_lease`, que
        -- só solta se ainda for ele.
        return row(false, null, null)::internal.turn_outcome;
    end if;

    -- Judge 1 refused the draft (S8). The turn is OVER — the sequence advanced
    -- and the lease is released above — and nothing goes out: no outbox row and
    -- no line in the transcript, because nothing was said. The trail of this
    -- silence is the `alerts` row the responder wrote before calling. Nothing
    -- to evaluate either: there is no reply.
    if p_content is null or jsonb_typeof(p_content) = 'null' then
        return row(true, null, null)::internal.turn_outcome;
    end if;

    select case type when 'cloud' then 'whatsapp_cloud' else 'whatsapp_evolution' end
      into v_channel
      from public.channels_accounts
     where id = v_channel_id;

    -- Tudo daqui para baixo comita com o UPDATE acima ou não comita nada. Uma
    -- conclusão sem linha na outbox é a versão outbound do "prazo perdido sem
    -- job": a conversa avança, o cliente nunca recebe, e nada aparece em lugar
    -- nenhum — a menos que seja o não-envio deliberado do ramo acima.
    insert into internal.message_outbox
        (tenant_id, conversation_id, contact_id, channel_account_id,
         kind, payload, idempotency_key)
    values
        (v_tenant_id, p_conversation_id, v_contact_id, v_channel_id,
         p_kind, p_content, p_idempotency_key)
    returning id into v_outbox_id;

    v_seq := internal.next_message_seq(p_conversation_id, 'outbound');

    insert into public.messages
        (tenant_id, conversation_id, direction, seq, channel,
         author_type, content, outbox_id)
    values
        (v_tenant_id, p_conversation_id, 'outbound', v_seq, v_channel,
         'agent', p_content, v_outbox_id)
    returning id into v_message_id;

    -- The evaluation job, atomic with the reply it evaluates (decisão 91).
    -- WHETHER to judge is not decided here: inside the shadow window everything
    -- is judged, outside it the risk gate decides — both in the consumer, where
    -- the clock and the decision are injectable and testable. SQL only makes
    -- sure the question is always asked.
    perform pgmq.send('q_evals', jsonb_build_object(
        'tenant_id', v_tenant_id,
        'conversation_id', p_conversation_id,
        'message_id', v_message_id
    ));

    return row(true, v_seq, v_outbox_id)::internal.turn_outcome;
end
$$;

comment on function internal.conclude_turn(uuid, uuid, integer, integer, integer, jsonb, text, text) is
    'Conclui o turno com o CAS estendido. Conteúdo nulo (SQL NULL ou jsonb null) '
    'significa "o Judge 1 reprovou": a conversa avança e NADA sai (S8). '
    'Toda resposta que SAI enfileira sua avaliação em q_evals na mesma '
    'transação (S9b, decisão 91).';


-- ## The correction path
--
-- A `critical` verdict after the fact means the customer already has a bad
-- message. The correction is an ordinary outbound: it goes through the outbox
-- and the sender like any other, and — in the consumer, before this is called —
-- through Judge 1 like any other. The 100% law has no side door.
--
-- No CAS here, and that is the whole point: there is no turn to conclude. The
-- conversation may be idle, or someone else may hold the lease; a correction
-- must not wait for either. The shape is `apply_domain_event`'s (decisão 74):
-- one transaction, outcomes as DATA, and the caller archives on every one of
-- them. An exception is reserved for what IS a bug — a message id that does not
-- exist.
--
-- Idempotency is the outbox's UNIQUE on `idempotency_key`, keyed by the message
-- being corrected: a redelivered evaluation job cannot produce a second
-- correction. That is the same second lock the funnel touch uses.

create type internal.correction_outcome as enum (
    'sent',            -- the correction is in the outbox
    'already_sent',    -- this message was already corrected; nothing to do
    'no_channel'       -- the account is gone: cannot send, and must not pretend
);

create function internal.enqueue_correction(
    p_message_id uuid,
    p_content    jsonb
)
    returns internal.correction_outcome
    language plpgsql
    set search_path = pg_catalog, public, internal
as $$
declare
    v_tenant_id       uuid;
    v_conversation_id uuid;
    v_contact_id      uuid;
    v_channel_id      uuid;
    v_channel         text;
    v_seq             integer;
    v_outbox_id       uuid;
    v_key             text;
begin
    select m.tenant_id, m.conversation_id, c.contact_id, c.channel_account_id
      into v_tenant_id, v_conversation_id, v_contact_id, v_channel_id
      from public.messages m
      join public.conversations c on c.id = m.conversation_id
     where m.id = p_message_id
       and m.direction = 'outbound';

    if not found then
        -- Not an outcome: a job pointing at a message that does not exist (or
        -- at an inbound one) is a bug in whoever enqueued it, and the retry
        -- ladder to the DLQ is where a human sees it.
        raise exception 'enqueue_correction: no outbound message %', p_message_id;
    end if;

    select case type when 'cloud' then 'whatsapp_cloud' else 'whatsapp_evolution' end
      into v_channel
      from public.channels_accounts
     where id = v_channel_id
       and status = 'active';

    if not found then
        return 'no_channel'::internal.correction_outcome;
    end if;

    v_key := 'correction-' || p_message_id::text;

    if exists (select 1 from internal.message_outbox where idempotency_key = v_key) then
        return 'already_sent'::internal.correction_outcome;
    end if;

    insert into internal.message_outbox
        (tenant_id, conversation_id, contact_id, channel_account_id,
         kind, payload, idempotency_key)
    values
        (v_tenant_id, v_conversation_id, v_contact_id, v_channel_id,
         'correction', p_content, v_key)
    returning id into v_outbox_id;

    v_seq := internal.next_message_seq(v_conversation_id, 'outbound');

    insert into public.messages
        (tenant_id, conversation_id, direction, seq, channel,
         author_type, content, outbox_id)
    values
        (v_tenant_id, v_conversation_id, 'outbound', v_seq, v_channel,
         'agent', p_content, v_outbox_id);

    return 'sent'::internal.correction_outcome;
end
$$;

comment on function internal.enqueue_correction(uuid, jsonb) is
    'Enfileira a auto-correção de uma resposta já enviada. Sem CAS: não há '
    'turno a concluir. Desfechos são dado; exceção só para message_id '
    'inexistente. A correção passa pelo Judge 1 ANTES desta chamada (S9b).';

revoke all on function internal.enqueue_correction(uuid, jsonb) from public;
grant execute on function internal.enqueue_correction(uuid, jsonb) to worker_role;

-- E2 · S8 — concluding a turn WITHOUT sending, for Judge 1 pre-send.
--
-- RF-015: every reply passes Judge 1 before it leaves. A refused draft must
-- never reach the outbox — and the conversation must still MOVE ON. Those two
-- are one problem: if the turn does not conclude, `last_processed_seq` stays
-- behind, the coalescer creates the job again, and the agent regenerates
-- against the same message forever, burning tokens on a reply that will never
-- be sent.
--
-- Expand-contract, additively: `create or replace` with the SAME signature. A
-- caller that passes content behaves exactly as it did before this migration
-- (the N-1 compatibility test lives in tests/db/test_conclude_without_send.py),
-- and the runtime of the previous release keeps working against this schema.
--
-- The comment inside the function warns that a conclusion with no outbox row is
-- "the customer never receiving and nothing appearing anywhere". That is right
-- for an ACCIDENT and this path is the opposite of one: the responder opens the
-- alert BEFORE concluding, so either both exist or neither does. The warning
-- stays in the body, next to the branch it is about.
--
-- Two shapes of "nothing to say" are honoured, not one. `Jsonb(None)` in
-- psycopg renders JSON `null`, not SQL NULL, so a caller who forgets that would
-- otherwise queue a message whose payload is the JSON value null — and the
-- sender would meet it at the worst possible moment. Both are refused a send.

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
    -- silence is the `alerts` row the responder wrote before calling.
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
         'agent', p_content, v_outbox_id);

    return row(true, v_seq, v_outbox_id)::internal.turn_outcome;
end
$$;

comment on function internal.conclude_turn(uuid, uuid, integer, integer, integer, jsonb, text, text) is
    'Conclui o turno com o CAS estendido. Conteúdo nulo (SQL NULL ou jsonb null) '
    'significa "o Judge 1 reprovou": a conversa avança e NADA sai (S8).';

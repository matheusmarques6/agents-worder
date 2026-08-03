-- E1 · A5 — claim_outbox_batch e os desfechos do envio.
--
-- O sender drena a outbox de TODOS os tenants — cross-tenant por natureza,
-- como o coalescer, e pelo mesmo motivo é SECURITY DEFINER no padrão de claim
-- function do ADR-11: search_path fixo, EXECUTE revogado de PUBLIC e concedido
-- só ao sender_role.
--
-- A assinatura é a defesa: não existe parâmetro de filtro. Um sender que
-- pudesse pedir "só as linhas do tenant X" seria uma consulta cross-tenant
-- arbitrária com outro nome.

create type internal.claimed_send as (
    outbox_id           uuid,
    tenant_id           uuid,
    channel_type        text,
    channel_external_id text,
    to_phone_e164       text,
    payload             jsonb,
    idempotency_key     text,
    attempt_count       integer
);

comment on type internal.claimed_send is
    'Tudo que um envio precisa, numa linha só. Sender que faz segunda query é sender que abre porta para inconsistência: entre a query e o envio, o mundo muda.';

create function internal.claim_outbox_batch(
    p_claim_token uuid,
    p_limit       integer default 50,
    p_lease       interval default interval '60 seconds'
)
    returns setof internal.claimed_send
    language sql
    security definer
    set search_path = pg_catalog, public, internal
as $$
    with claimed as (
        select id
          from internal.message_outbox
         where status = 'pending'
           and next_attempt_at <= now()
         order by next_attempt_at
         for update skip locked
         limit p_limit
    ),
    marked as (
        update internal.message_outbox o
           set status = 'sending',
               locked_by = p_claim_token::text,
               locked_until = now() + p_lease,
               request_started_at = now(),
               attempt_count = o.attempt_count + 1
          from claimed
         where o.id = claimed.id
        returning o.*
    )
    select m.id,
           m.tenant_id,
           ch.type,
           ch.external_account_id,
           ct.phone_e164,
           m.payload,
           m.idempotency_key,
           m.attempt_count
      from marked m
      join public.channels_accounts ch on ch.id = m.channel_account_id
      join public.contacts ct on ct.id = m.contact_id
$$;

-- Os dois desfechos que este PR conhece. `sending → unknown → sent |
-- manual_review` — o caminho do processo que morreu no meio — fica para o
-- PR dos cenários C, onde tem teste.
--
-- Só com o token do dono: a disciplina da lease, repetida. Um sender atrasado
-- que pudesse marcar como enviado o item que outro sender re-reivindicou
-- transformaria uma reentrega em estado mentiroso.
create function internal.mark_outbox_sent(
    p_outbox_id           uuid,
    p_claim_token         uuid,
    p_provider_message_id text
)
    returns boolean
    language plpgsql
    security definer
    set search_path = pg_catalog, internal
as $$
begin
    update internal.message_outbox
       set status = 'sent',
           sent_at = now(),
           provider_message_id = p_provider_message_id,
           locked_by = null,
           locked_until = null,
           last_error = null
     where id = p_outbox_id
       and status = 'sending'
       and locked_by = p_claim_token::text;

    return found;
end
$$;

create function internal.mark_outbox_failed(
    p_outbox_id   uuid,
    p_claim_token uuid,
    p_transient   boolean,
    p_error       text,
    -- O atraso vem do runtime, que é quem tem o backoff com o acaso injetado
    -- (unidade 4). O SQL não recalcula a escada — recalculá-la aqui seria uma
    -- segunda cópia dos números canônicos esperando divergir.
    p_retry_in    interval default interval '30 seconds'
)
    returns boolean
    language plpgsql
    security definer
    set search_path = pg_catalog, internal
as $$
begin
    if p_transient then
        update internal.message_outbox
           set status = 'pending',
               next_attempt_at = now() + p_retry_in,
               locked_by = null,
               locked_until = null,
               last_error = p_error
         where id = p_outbox_id
           and status = 'sending'
           and locked_by = p_claim_token::text;
    else
        update internal.message_outbox
           set status = 'failed',
               locked_by = null,
               locked_until = null,
               last_error = p_error
         where id = p_outbox_id
           and status = 'sending'
           and locked_by = p_claim_token::text;
    end if;

    return found;
end
$$;

-- ---------------------------------------------------------------------------
-- Quem executa: só o sender. O worker escreve na outbox (PR-0); quem a drena
-- é outro papel, e a assimetria tem teste nos dois sentidos.
-- ---------------------------------------------------------------------------
revoke execute on function internal.claim_outbox_batch(uuid, integer, interval) from public;
revoke execute on function internal.mark_outbox_sent(uuid, uuid, text) from public;
revoke execute on function
    internal.mark_outbox_failed(uuid, uuid, boolean, text, interval) from public;

grant execute on function internal.claim_outbox_batch(uuid, integer, interval) to sender_role;
grant execute on function internal.mark_outbox_sent(uuid, uuid, text) to sender_role;
grant execute on function
    internal.mark_outbox_failed(uuid, uuid, boolean, text, interval) to sender_role;

// Ingestão · WhatsApp Cloud API — a porta de entrada HTTP do motor.
//
// O desenho inteiro cabe numa frase da arquitetura: validar a assinatura,
// fazer UMA chamada a `internal.ingest_webhook()`, responder 200 em
// milissegundos. Nada de negócio mora aqui — atomicidade, idempotência e a
// resolução do tenant são da função SQL, provadas pela suíte A1.
//
// Fronteiras de confiança, na ordem em que mordem:
//   1. assinatura HMAC-SHA256 do corpo CRU, verificada ANTES de tocar o banco
//      (worder1 verificava e seguia mesmo assim; aqui inválida = 401, sempre);
//   2. schema estrito via zod — campo inesperado é descartado, tipo errado é
//      rejeitado, nunca "usa o que parseou";
//   3. o tenant vem do `phone_number_id` (a conta de origem), jamais do corpo.
//
// Identidade no banco: `ingestion_role`, por conexão direta (o schema
// `internal` está fora da Data API por construção — não existe RPC para ele).
// O LOGIN do papel é concedido fora de banda, por ambiente (decisão 14), e a
// credencial chega por secret. Sem secret, a função falha FECHADA.
//
// This file is the HTTP seam and nothing else: environment, a database handle,
// and the adapter that turns the handler's two decisions into two SQL calls.
// The boundaries above live in `_lib/` — signature, schema, branch decision —
// where `deno test` can reach them.

import postgres from "npm:postgres@3.4.5";

import { type Deps, handleRequest, type IngestPort } from "./_lib/handler.ts";
import { messageContent } from "./_lib/payload.ts";

// --- ambiente (fail closed: sem segredo, sem serviço) -------------------------

function required(name: string): string {
  const value = Deno.env.get(name);
  if (!value) throw new Error(`${name} não está definido — a ingestão não sobe sem ele`);
  return value;
}

const APP_SECRET = required("META_APP_SECRET");
const VERIFY_TOKEN = required("META_VERIFY_TOKEN");

const sql = postgres(required("INGESTION_DB_URL"), {
  max: 3,
  prepare: false, // pooler em modo transação não suporta prepared statements
});

// --- the adapter ----------------------------------------------------------------

const port: IngestPort = {
  async ingestMessage(phoneNumberId, message) {
    await sql`
      select * from internal.ingest_webhook(
        'meta',
        ${phoneNumberId},
        ${message.id},
        'message_inbound',
        ${sql.json(messageContent(message))}
      )
    `;
    // `unknown_account` e `duplicate` são desfechos, não erros: a função decidiu,
    // o evento tem rastro (ou deliberadamente não tem), e a Meta recebe 200 para
    // não reentregar eternamente o que nunca vai mudar de resultado.
  },

  async correlateStatus(correlation) {
    await sql`
      select internal.correlate_outbox_status(
        ${correlation.opaqueData},
        ${correlation.status},
        ${correlation.wamid}
      )
    `;
  },
};

const deps: Deps = { appSecret: APP_SECRET, verifyToken: VERIFY_TOKEN, port };

Deno.serve((request: Request) => handleRequest(request, deps));

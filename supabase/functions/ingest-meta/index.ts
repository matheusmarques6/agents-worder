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

import postgres from "npm:postgres@3.4.5";
import { z } from "npm:zod@3.23.8";

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

// --- assinatura ---------------------------------------------------------------

const encoder = new TextEncoder();

async function signatureIsValid(rawBody: string, header: string | null): Promise<boolean> {
  if (!header || !header.startsWith("sha256=")) return false;

  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(APP_SECRET),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const digest = await crypto.subtle.sign("HMAC", key, encoder.encode(rawBody));
  const expected = Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");

  // Comparação em tempo constante: um comparador que desiste no primeiro byte
  // diferente vaza, byte a byte, quanto da assinatura o atacante acertou.
  const given = header.slice("sha256=".length);
  if (given.length !== expected.length) return false;
  let diff = 0;
  for (let index = 0; index < expected.length; index++) {
    diff |= given.charCodeAt(index) ^ expected.charCodeAt(index);
  }
  return diff === 0;
}

// --- o schema estrito ----------------------------------------------------------
// Só o que a ingestão consome. Todo o resto do payload gigante da Meta é
// descartado na entrada — o bruto que a plataforma guarda é o que a função SQL
// recebe como `p_payload`, e ELE é montado aqui, não copiado às cegas.

const InboundMessage = z.object({
  id: z.string().min(1), // wamid — a chave natural do evento
  from: z.string().regex(/^\d{8,15}$/), // dígitos, sem '+': a Meta manda assim
  timestamp: z.string().optional(),
  type: z.string(),
  text: z.object({ body: z.string() }).optional(),
});

const StatusUpdate = z.object({
  id: z.string().min(1),
  status: z.enum(["sent", "delivered", "read", "failed"]),
  biz_opaque_callback_data: z.string().optional(),
});

const ChangeValue = z.object({
  metadata: z.object({ phone_number_id: z.string().min(1) }),
  messages: z.array(InboundMessage).optional(),
  statuses: z.array(StatusUpdate).optional(),
});

const WebhookBody = z.object({
  object: z.string(),
  entry: z
    .array(
      z.object({
        changes: z
          .array(z.object({ field: z.string(), value: z.unknown() }))
          .optional(),
      }),
    )
    .optional(),
});

// --- handlers -------------------------------------------------------------------

async function handleVerification(url: URL): Promise<Response> {
  const mode = url.searchParams.get("hub.mode");
  const token = url.searchParams.get("hub.verify_token");
  const challenge = url.searchParams.get("hub.challenge");

  if (mode !== "subscribe" || token !== VERIFY_TOKEN || !challenge) {
    return new Response("forbidden", { status: 403 });
  }
  return new Response(challenge, { status: 200 });
}

async function ingestMessage(phoneNumberId: string, message: z.infer<typeof InboundMessage>) {
  await sql`
    select * from internal.ingest_webhook(
      'meta',
      ${phoneNumberId},
      ${message.id},
      'message_inbound',
      ${sql.json({
        from: `+${message.from}`,
        message: { type: message.type, text: message.text?.body ?? null },
      })}
    )
  `;
  // `unknown_account` e `duplicate` são desfechos, não erros: a função decidiu,
  // o evento tem rastro (ou deliberadamente não tem), e a Meta recebe 200 para
  // não reentregar eternamente o que nunca vai mudar de resultado.
}

async function correlateStatus(status: z.infer<typeof StatusUpdate>) {
  // Sem a chave opaca não há o que correlacionar — mensagens de outras origens
  // (ou anteriores a nós) passam batido, de propósito.
  if (!status.biz_opaque_callback_data) return;
  if (status.status === "sent") return; // 'delivered'/'read'/'failed' decidem; 'sent' cru não

  const mapped = status.status === "failed" ? "failed" : "sent";
  await sql`
    select internal.correlate_outbox_status(
      ${status.biz_opaque_callback_data},
      ${mapped},
      ${status.id}
    )
  `;
}

async function handleEvents(request: Request): Promise<Response> {
  const rawBody = await request.text();

  if (!(await signatureIsValid(rawBody, request.headers.get("x-hub-signature-256")))) {
    return new Response("invalid signature", { status: 401 });
  }

  let body: z.infer<typeof WebhookBody>;
  try {
    body = WebhookBody.parse(JSON.parse(rawBody));
  } catch {
    // Assinada pela Meta mas fora do formato: rejeitar alto. 400 sem retry é
    // melhor que "usa o que parseou".
    return new Response("malformed payload", { status: 400 });
  }

  if (body.object !== "whatsapp_business_account") {
    return new Response(JSON.stringify({ ignored: true }), { status: 200 });
  }

  for (const entry of body.entry ?? []) {
    for (const change of entry.changes ?? []) {
      if (change.field !== "messages") continue;

      const parsed = ChangeValue.safeParse(change.value);
      if (!parsed.success) continue; // estrito: o que não valida não entra

      const { metadata, messages, statuses } = parsed.data;
      for (const message of messages ?? []) {
        await ingestMessage(metadata.phone_number_id, message);
      }
      for (const status of statuses ?? []) {
        await correlateStatus(status);
      }
    }
  }

  return new Response(JSON.stringify({ received: true }), { status: 200 });
}

Deno.serve(async (request: Request) => {
  const url = new URL(request.url);

  if (request.method === "GET") return handleVerification(url);
  if (request.method === "POST") {
    try {
      return await handleEvents(request);
    } catch (error) {
      // Erro NOSSO (banco fora, bug): 500 faz a Meta reentregar, e a
      // idempotência da ingestão faz a reentrega ser segura. É o único lugar
      // onde "tente de novo" é a resposta certa para o remetente.
      console.error("[ingest-meta]", error);
      return new Response("internal error", { status: 500 });
    }
  }
  return new Response("method not allowed", { status: 405 });
});

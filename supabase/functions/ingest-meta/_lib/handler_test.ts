// The four branches, and what each one costs.
//
// A status code here is a sentence to Meta: 401 "you are not who you say", 500
// "our fault, send it again", 200 "stop sending this" — whether we handled it,
// ignored it on purpose, or could not parse it and never will. Getting one
// wrong is either an event redelivered forever or an event lost in silence, so
// each branch gets a test that also checks whether the database was touched.

import { assert, assertEquals } from "jsr:@std/assert@1.0.0";

import { type Deps, handleRequest, type IngestPort } from "./handler.ts";
import type { InboundMessage } from "./schema.ts";
import type { StatusCorrelation } from "./payload.ts";
import { signatureHeaderFor } from "./signature.ts";

const APP_SECRET = "app-secret-do-tenant";
const VERIFY_TOKEN = "token-de-verificacao";
const PHONE_NUMBER_ID = "1234567890";
const URL_ = "https://projeto.supabase.co/functions/v1/ingest-meta";

interface Recorder extends IngestPort {
  readonly ingested: Array<{ phoneNumberId: string; message: InboundMessage }>;
  readonly correlated: StatusCorrelation[];
}

function recorder(): Recorder {
  const ingested: Array<{ phoneNumberId: string; message: InboundMessage }> = [];
  const correlated: StatusCorrelation[] = [];
  return {
    ingested,
    correlated,
    ingestMessage(phoneNumberId, message) {
      ingested.push({ phoneNumberId, message });
      return Promise.resolve();
    },
    correlateStatus(correlation) {
      correlated.push(correlation);
      return Promise.resolve();
    },
  };
}

function deps(port: IngestPort, onError?: (error: unknown) => void): Deps {
  return { appSecret: APP_SECRET, verifyToken: VERIFY_TOKEN, port, onError };
}

async function signedPost(body: unknown): Promise<Request> {
  const rawBody = typeof body === "string" ? body : JSON.stringify(body);
  return new Request(URL_, {
    method: "POST",
    body: rawBody,
    headers: { "x-hub-signature-256": await signatureHeaderFor(rawBody, APP_SECRET) },
  });
}

function envelope(value: unknown, field = "messages") {
  return { object: "whatsapp_business_account", entry: [{ changes: [{ field, value }] }] };
}

const TEXT_EVENT = envelope({
  metadata: { display_phone_number: "551133334444", phone_number_id: PHONE_NUMBER_ID },
  messages: [{
    id: "wamid.TEXTO",
    from: "5511999998888",
    timestamp: "1717171717",
    type: "text",
    text: { body: "cadê meu pedido?" },
  }],
});

// --- 401: not Meta -------------------------------------------------------------

Deno.test("an unsigned request is 401 and never reaches the database", async () => {
  const port = recorder();
  const request = new Request(URL_, { method: "POST", body: JSON.stringify(TEXT_EVENT) });

  const response = await handleRequest(request, deps(port));

  assertEquals(response.status, 401);
  assertEquals(port.ingested.length, 0);
});

Deno.test("a request signed with the wrong secret is 401", async () => {
  const port = recorder();
  const rawBody = JSON.stringify(TEXT_EVENT);
  const request = new Request(URL_, {
    method: "POST",
    body: rawBody,
    headers: { "x-hub-signature-256": await signatureHeaderFor(rawBody, "outro-segredo") },
  });

  const response = await handleRequest(request, deps(port));

  assertEquals(response.status, 401);
  assertEquals(port.ingested.length, 0);
});

Deno.test("a valid signature over a different body is 401", async () => {
  const port = recorder();
  const request = new Request(URL_, {
    method: "POST",
    body: JSON.stringify(TEXT_EVENT),
    headers: { "x-hub-signature-256": await signatureHeaderFor("{}", APP_SECRET) },
  });

  const response = await handleRequest(request, deps(port));

  assertEquals(response.status, 401);
  assertEquals(port.ingested.length, 0);
});

// --- 200 + a report: Meta's error -------------------------------------------------
//
// Nothing is ingested, and the answer is still 200: no redelivery of an
// unparseable envelope will ever parse, and every non-200 buys days of retries.

Deno.test("a signed body that is not JSON is acknowledged, not ingested", async () => {
  const port = recorder();
  const errors: unknown[] = [];

  const response = await handleRequest(
    await signedPost("nao é json {"),
    deps(port, (error) => errors.push(error)),
  );

  assertEquals(response.status, 200);
  assertEquals(await response.json(), { ignored: true, reason: "malformed" });
  assertEquals(port.ingested.length, 0);
  assertEquals(errors.length, 1); // acknowledged is not the same as unnoticed
});

Deno.test("a signed body off-format is acknowledged, not ingested", async () => {
  const port = recorder();
  const errors: unknown[] = [];
  const bodies: unknown[] = [
    { entry: [] }, // no `object`
    { object: 1, entry: [] },
    { object: "whatsapp_business_account", entry: "nada" },
  ];

  for (const body of bodies) {
    const response = await handleRequest(
      await signedPost(body),
      deps(port, (error) => errors.push(error)),
    );
    assertEquals(response.status, 200, JSON.stringify(body));
    assertEquals(await response.json(), { ignored: true, reason: "malformed" });
  }

  assertEquals(port.ingested.length, 0);
  assertEquals(errors.length, bodies.length);
});

// --- 500: our error -------------------------------------------------------------

Deno.test("a database that fails is 500 — Meta redelivers, ingestion is idempotent", async () => {
  const errors: unknown[] = [];
  const broken: IngestPort = {
    ingestMessage() {
      return Promise.reject(new Error("connection refused"));
    },
    correlateStatus() {
      return Promise.resolve();
    },
  };

  const response = await handleRequest(
    await signedPost(TEXT_EVENT),
    deps(broken, (error) => errors.push(error)),
  );

  assertEquals(response.status, 500);
  assertEquals(errors.length, 1);
  assert(errors[0] instanceof Error);
});

Deno.test("a signature check that throws is 500, not a silent 200", async () => {
  const port = recorder();
  const broken = new Request(URL_, { method: "POST" });
  // A body that cannot be read at all — the failure happens before any branch.
  Object.defineProperty(broken, "text", {
    value: () => Promise.reject(new Error("stream closed")),
  });

  const response = await handleRequest(broken, deps(port, () => {}));

  assertEquals(response.status, 500);
  assertEquals(port.ingested.length, 0);
});

// --- 200: handled ---------------------------------------------------------------

Deno.test("a signed message is ingested and answered 200", async () => {
  const port = recorder();

  const response = await handleRequest(await signedPost(TEXT_EVENT), deps(port));

  assertEquals(response.status, 200);
  assertEquals(await response.json(), { received: true });
  assertEquals(port.ingested.length, 1);
  // The tenant comes from the origin account, never from the body.
  assertEquals(port.ingested[0].phoneNumberId, PHONE_NUMBER_ID);
  assertEquals(port.ingested[0].message.id, "wamid.TEXTO");
});

Deno.test("a tap on Bloquear crosses the door whole", async () => {
  const port = recorder();
  const event = envelope({
    metadata: { phone_number_id: PHONE_NUMBER_ID },
    messages: [{
      id: "wamid.BOTAO",
      from: "5511999998888",
      type: "interactive",
      interactive: {
        type: "button_reply",
        // The id from `dispatch/consent.py`, echoed back by the platform.
        button_reply: { id: "consent_block", title: "Bloquear" },
      },
    }],
  });

  const response = await handleRequest(await signedPost(event), deps(port));

  assertEquals(response.status, 200);
  assertEquals(port.ingested.length, 1);
  assertEquals(port.ingested[0].message.interactive?.button_reply, {
    id: "consent_block",
    title: "Bloquear",
  });
});

Deno.test("a status update is correlated, and 'sent' is not", async () => {
  const port = recorder();
  const event = envelope({
    metadata: { phone_number_id: PHONE_NUMBER_ID },
    statuses: [
      { id: "wamid.A", status: "sent", biz_opaque_callback_data: "outbox-1" },
      { id: "wamid.B", status: "delivered", biz_opaque_callback_data: "outbox-2" },
      { id: "wamid.C", status: "failed" },
      { id: "wamid.D", status: "failed", biz_opaque_callback_data: "outbox-4" },
    ],
  });

  const response = await handleRequest(await signedPost(event), deps(port));

  assertEquals(response.status, 200);
  assertEquals(port.correlated, [
    { opaqueData: "outbox-2", status: "sent", wamid: "wamid.B" },
    { opaqueData: "outbox-4", status: "failed", wamid: "wamid.D" },
  ]);
});

Deno.test("another Meta product on the same endpoint is acknowledged and ignored", async () => {
  const port = recorder();
  const event = { object: "page", entry: [{ changes: [{ field: "messages", value: {} }] }] };

  const response = await handleRequest(await signedPost(event), deps(port));

  assertEquals(response.status, 200);
  assertEquals(await response.json(), { ignored: true });
  assertEquals(port.ingested.length, 0);
});

Deno.test("a field we do not read is skipped, and the rest still runs", async () => {
  const port = recorder();
  const event = {
    object: "whatsapp_business_account",
    entry: [
      { changes: [{ field: "message_template_status_update", value: { anything: true } }] },
      TEXT_EVENT.entry[0],
    ],
  };

  const response = await handleRequest(await signedPost(event), deps(port));

  assertEquals(response.status, 200);
  assertEquals(port.ingested.length, 1);
});

Deno.test("a change that does not validate is dropped, and the rest still runs", async () => {
  const port = recorder();
  const event = {
    object: "whatsapp_business_account",
    entry: [{
      changes: [
        // No `metadata`: there is no origin account, so there is no tenant.
        { field: "messages", value: { messages: [{ id: "w", from: "5511999998888", type: "text" }] } },
        TEXT_EVENT.entry[0].changes[0],
      ],
    }],
  };

  const response = await handleRequest(await signedPost(event), deps(port));

  assertEquals(response.status, 200);
  assertEquals(port.ingested.length, 1);
  assertEquals(port.ingested[0].message.id, "wamid.TEXTO");
});

Deno.test("an empty envelope is 200 and touches nothing", async () => {
  const port = recorder();

  const response = await handleRequest(
    await signedPost({ object: "whatsapp_business_account" }),
    deps(port),
  );

  assertEquals(response.status, 200);
  assertEquals(port.ingested.length, 0);
  assertEquals(port.correlated.length, 0);
});

// --- GET and everything else -----------------------------------------------------

Deno.test("the subscription handshake echoes the challenge", async () => {
  const port = recorder();
  const url =
    `${URL_}?hub.mode=subscribe&hub.verify_token=${VERIFY_TOKEN}&hub.challenge=1158201444`;

  const response = await handleRequest(new Request(url), deps(port));

  assertEquals(response.status, 200);
  assertEquals(await response.text(), "1158201444");
});

Deno.test("a handshake with the wrong token is 403", async () => {
  const port = recorder();
  const cases = [
    `${URL_}?hub.mode=subscribe&hub.verify_token=chutado&hub.challenge=1`,
    `${URL_}?hub.mode=unsubscribe&hub.verify_token=${VERIFY_TOKEN}&hub.challenge=1`,
    `${URL_}?hub.mode=subscribe&hub.verify_token=${VERIFY_TOKEN}`,
    URL_,
  ];

  for (const url of cases) {
    const response = await handleRequest(new Request(url), deps(port));
    assertEquals(response.status, 403, url);
    await response.body?.cancel();
  }
});

Deno.test("any other method is 405", async () => {
  const port = recorder();

  for (const method of ["PUT", "DELETE", "PATCH"]) {
    const response = await handleRequest(new Request(URL_, { method }), deps(port));
    assertEquals(response.status, 405, method);
    await response.body?.cancel();
  }
});

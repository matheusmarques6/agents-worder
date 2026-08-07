// The second boundary, under test: what Meta sends is not what the ingestion
// consumes. An unexpected field is dropped, a wrong type is a rejection, and a
// half-formed event never becomes a half-formed row.

import { assert, assertEquals, assertFalse } from "jsr:@std/assert@1.0.0";

import {
  ButtonReply,
  ChangeValue,
  InboundMessage,
  StatusUpdate,
  WebhookBody,
} from "./schema.ts";

const TEXT_MESSAGE = {
  id: "wamid.HBgNNTUxMTk5",
  from: "5511999998888",
  timestamp: "1717171717",
  type: "text",
  text: { body: "oi, e o meu pedido?" },
};

Deno.test("an unexpected field is dropped, not carried", () => {
  const parsed = InboundMessage.parse({
    ...TEXT_MESSAGE,
    // Real fields Meta sends and the ingestion has no business storing.
    context: { from: "5511888887777", id: "wamid.OUTRO" },
    referral: { source_url: "https://exemplo.com" },
    forwarded: true,
  });

  assertEquals(Object.keys(parsed).sort(), ["from", "id", "text", "timestamp", "type"]);
  assertFalse("context" in parsed);
  assertFalse("referral" in parsed);
});

Deno.test("a wrong type is refused, never coerced", () => {
  assertFalse(InboundMessage.safeParse({ ...TEXT_MESSAGE, from: 5511999998888 }).success);
  assertFalse(InboundMessage.safeParse({ ...TEXT_MESSAGE, type: 42 }).success);
  assertFalse(InboundMessage.safeParse({ ...TEXT_MESSAGE, text: "oi" }).success);
  assertFalse(InboundMessage.safeParse({ ...TEXT_MESSAGE, text: { body: 7 } }).success);
});

Deno.test("an incomplete message is refused", () => {
  const { id: _id, ...withoutId } = TEXT_MESSAGE;
  assertFalse(InboundMessage.safeParse(withoutId).success);

  const { from: _from, ...withoutFrom } = TEXT_MESSAGE;
  assertFalse(InboundMessage.safeParse(withoutFrom).success);

  const { type: _type, ...withoutType } = TEXT_MESSAGE;
  assertFalse(InboundMessage.safeParse(withoutType).success);

  assertFalse(InboundMessage.safeParse({ ...TEXT_MESSAGE, id: "" }).success);
  assertFalse(InboundMessage.safeParse(null).success);
  assertFalse(InboundMessage.safeParse("oi").success);
});

Deno.test("the sender is digits only — Meta's format, not E.164", () => {
  assert(InboundMessage.safeParse({ ...TEXT_MESSAGE, from: "5511999998888" }).success);
  assertFalse(InboundMessage.safeParse({ ...TEXT_MESSAGE, from: "+5511999998888" }).success);
  assertFalse(InboundMessage.safeParse({ ...TEXT_MESSAGE, from: "55 11 99999-8888" }).success);
  assertFalse(InboundMessage.safeParse({ ...TEXT_MESSAGE, from: "1234567" }).success); // < 8
  assertFalse(InboundMessage.safeParse({ ...TEXT_MESSAGE, from: "1234567890123456" }).success);
});

Deno.test("a button reply keeps its id and drops the rest", () => {
  const parsed = ButtonReply.parse({
    id: "consent_block",
    title: "Bloquear",
    payload: "algo que a Meta inventou depois",
  });

  assertEquals(parsed, { id: "consent_block", title: "Bloquear" });
  assertFalse(ButtonReply.safeParse({ id: "" }).success);
  assertFalse(ButtonReply.safeParse({ title: "Bloquear" }).success);
  assertFalse(ButtonReply.safeParse({ id: 1 }).success);
});

Deno.test("an interactive message survives with the reply intact", () => {
  const parsed = InboundMessage.parse({
    id: "wamid.BOTAO",
    from: "5511999998888",
    type: "interactive",
    interactive: {
      type: "button_reply",
      button_reply: { id: "consent_block", title: "Bloquear" },
    },
  });

  assertEquals(parsed.interactive?.button_reply?.id, "consent_block");
});

Deno.test("an interactive message without a reply is still a message", () => {
  const parsed = InboundMessage.parse({
    id: "wamid.LISTA",
    from: "5511999998888",
    type: "interactive",
    interactive: { type: "list_reply" },
  });

  assertEquals(parsed.interactive?.button_reply, undefined);
});

Deno.test("only the four statuses Meta defines are accepted", () => {
  for (const status of ["sent", "delivered", "read", "failed"]) {
    assert(StatusUpdate.safeParse({ id: "wamid.X", status }).success, status);
  }
  assertFalse(StatusUpdate.safeParse({ id: "wamid.X", status: "queued" }).success);
  assertFalse(StatusUpdate.safeParse({ id: "wamid.X", status: "SENT" }).success);
  assertFalse(StatusUpdate.safeParse({ status: "sent" }).success);
});

Deno.test("a change value without the origin account is refused", () => {
  assertFalse(ChangeValue.safeParse({ messages: [TEXT_MESSAGE] }).success);
  assertFalse(ChangeValue.safeParse({ metadata: {}, messages: [TEXT_MESSAGE] }).success);
  assertFalse(
    ChangeValue.safeParse({ metadata: { phone_number_id: "" }, messages: [TEXT_MESSAGE] }).success,
  );

  // The tenant comes from here and from nowhere else — a `tenant_id` in the
  // body is not even parsed, let alone believed.
  const parsed = ChangeValue.parse({
    metadata: { display_phone_number: "5511333", phone_number_id: "1234567890" },
    tenant_id: "00000000-0000-0000-0000-000000000000",
    messages: [TEXT_MESSAGE],
  });
  assertEquals(parsed.metadata, { phone_number_id: "1234567890" });
  assertFalse("tenant_id" in parsed);
});

Deno.test("one bad message poisons its whole change", () => {
  // Deliberate: `messages` is an array schema, so a single invalid entry fails
  // the parse and `handleEvents` skips the change entirely — reporting how much
  // it dropped. Partial ingestion of a change Meta will redeliver is worse than
  // none; silent partial loss is worse than both.
  assertFalse(
    ChangeValue.safeParse({
      metadata: { phone_number_id: "1234567890" },
      messages: [TEXT_MESSAGE, { id: "wamid.RUIM", from: "nao-e-numero", type: "text" }],
    }).success,
  );
});

Deno.test("the envelope tolerates what it does not read", () => {
  const parsed = WebhookBody.parse({
    object: "whatsapp_business_account",
    entry: [{ id: "123", time: 1717171717, changes: [{ field: "messages", value: { a: 1 } }] }],
  });

  assertEquals(parsed.object, "whatsapp_business_account");
  assertEquals(parsed.entry?.[0].changes?.[0].field, "messages");
  assertFalse("id" in parsed.entry![0]);

  // But not a broken envelope.
  assertFalse(WebhookBody.safeParse({ entry: [] }).success);
  assertFalse(WebhookBody.safeParse({ object: "whatsapp_business_account", entry: {} }).success);
  assertFalse(WebhookBody.safeParse({ object: 1 }).success);
});

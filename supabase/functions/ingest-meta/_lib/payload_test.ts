// What the door hands to the database.
//
// The consent half of this file is new behaviour, not a net over old: a tap on
// Bloquear arrives here as an id WE issued, and `dispatch.consent.recognize`
// matches it against a table of two entries. Any translation on the way — a
// rename, a nesting, a title used instead of an id — and the refusal is not
// recognised, the contact keeps receiving, and nothing fails loudly.

import { assertEquals } from "jsr:@std/assert@^1.0.0";

import { InboundMessage, StatusUpdate } from "./schema.ts";
import { messageContent, statusCorrelation } from "./payload.ts";

// The two ids, verbatim from `runtime/src/agents_runtime/dispatch/consent.py`
// (`AUTHORIZE_BUTTON_ID` / `BLOCK_BUTTON_ID`). Written out rather than imported
// because the whole risk is the two sides drifting apart, and a shared constant
// would make a rename look correct from both ends.
const AUTHORIZE_BUTTON_ID = "consent_authorize";
const BLOCK_BUTTON_ID = "consent_block";

function tap(buttonId: string, title: string) {
  return InboundMessage.parse({
    id: "wamid.BOTAO",
    from: "5511999998888",
    type: "interactive",
    interactive: {
      type: "button_reply",
      button_reply: { id: buttonId, title },
    },
  });
}

Deno.test("a text message becomes the content row the engine reads", () => {
  const message = InboundMessage.parse({
    id: "wamid.TEXTO",
    from: "5511999998888",
    type: "text",
    text: { body: "quero cancelar" },
  });

  assertEquals(messageContent(message), {
    from: "+5511999998888", // E.164 is built here; Meta sends bare digits
    message: { type: "text", text: "quero cancelar", button_reply: null },
  });
});

Deno.test("a tap on Bloquear reaches the row verbatim", () => {
  const content = messageContent(tap(BLOCK_BUTTON_ID, "Bloquear"));

  // `recognize` reads `content["button_reply"]["id"]` — one level under the
  // message, next to `text`, never under `interactive`.
  assertEquals(content.message.button_reply, { id: "consent_block", title: "Bloquear" });
  assertEquals(content.message.button_reply?.id, BLOCK_BUTTON_ID);
  assertEquals(content.message.text, null); // a tap carries no text, and that is fine
  assertEquals(content.message.type, "interactive");
});

Deno.test("a tap on Autorizar reaches the row verbatim", () => {
  const content = messageContent(tap(AUTHORIZE_BUTTON_ID, "Autorizar"));

  assertEquals(content.message.button_reply, { id: "consent_authorize", title: "Autorizar" });
});

Deno.test("an id we never issued travels unchanged — it is just a message", () => {
  // The door does not judge. `recognize` returns None for anything outside its
  // two entries, and the turn proceeds as an ordinary message.
  const content = messageContent(tap("bloquear", "Bloquear"));

  assertEquals(content.message.button_reply, { id: "bloquear", title: "Bloquear" });
});

Deno.test("an interactive message that is not a tap carries no reply", () => {
  const message = InboundMessage.parse({
    id: "wamid.LISTA",
    from: "5511999998888",
    type: "interactive",
    interactive: { type: "list_reply" },
  });

  assertEquals(messageContent(message).message.button_reply, null);
});

Deno.test("delivered, read and failed are the statuses that decide", () => {
  const base = { id: "wamid.SAIDA", biz_opaque_callback_data: "outbox-42" };

  assertEquals(statusCorrelation(StatusUpdate.parse({ ...base, status: "delivered" })), {
    opaqueData: "outbox-42",
    status: "sent",
    wamid: "wamid.SAIDA",
  });
  assertEquals(statusCorrelation(StatusUpdate.parse({ ...base, status: "read" }))?.status, "sent");
  assertEquals(
    statusCorrelation(StatusUpdate.parse({ ...base, status: "failed" }))?.status,
    "failed",
  );
});

Deno.test("a raw 'sent' correlates nothing", () => {
  const status = StatusUpdate.parse({
    id: "wamid.SAIDA",
    status: "sent",
    biz_opaque_callback_data: "outbox-42",
  });

  assertEquals(statusCorrelation(status), null);
});

Deno.test("without our opaque key there is nothing of ours to correlate", () => {
  assertEquals(statusCorrelation(StatusUpdate.parse({ id: "w", status: "delivered" })), null);
  assertEquals(
    statusCorrelation(
      StatusUpdate.parse({ id: "w", status: "failed", biz_opaque_callback_data: "" }),
    ),
    null,
  );
});

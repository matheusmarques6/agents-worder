// The strict schema — the second trust boundary.
//
// Only what the ingestion consumes. Everything else in Meta's enormous payload
// is dropped at the door: an unexpected field never survives the parse, a wrong
// type is a rejection, and there is no "use what parsed".
//
// Moved here verbatim from `index.ts`. Nothing about the shapes changed.

import { z } from "npm:zod@3.23.8";

// The button reply (RF-033a). It is transport, not rule: the ingestion carries
// the `id` WE issued and nothing else — the one who recognises it is
// `agents_runtime.dispatch.consent`, deterministic, on the inbound turn.
// Without these four lines a tap on Bloquear would arrive as an `interactive`
// message with no text, and the contact's consent would vanish at the door.
export const ButtonReply = z.object({
  id: z.string().min(1),
  title: z.string().optional(),
});
export type ButtonReply = z.infer<typeof ButtonReply>;

export const InboundMessage = z.object({
  id: z.string().min(1), // wamid — the event's natural key
  from: z.string().regex(/^\d{8,15}$/), // digits, no '+': that is how Meta sends it
  timestamp: z.string().optional(),
  type: z.string(),
  text: z.object({ body: z.string() }).optional(),
  interactive: z
    .object({
      type: z.string(),
      button_reply: ButtonReply.optional(),
    })
    .optional(),
});
export type InboundMessage = z.infer<typeof InboundMessage>;

export const StatusUpdate = z.object({
  id: z.string().min(1),
  status: z.enum(["sent", "delivered", "read", "failed"]),
  biz_opaque_callback_data: z.string().optional(),
});
export type StatusUpdate = z.infer<typeof StatusUpdate>;

export const ChangeValue = z.object({
  metadata: z.object({ phone_number_id: z.string().min(1) }),
  messages: z.array(InboundMessage).optional(),
  statuses: z.array(StatusUpdate).optional(),
});
export type ChangeValue = z.infer<typeof ChangeValue>;

export const WebhookBody = z.object({
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
export type WebhookBody = z.infer<typeof WebhookBody>;

/** The `object` value that means "this is a WhatsApp webhook". Anything else is
 * acknowledged and ignored — Meta subscribes one endpoint to many products. */
export const WHATSAPP_OBJECT = "whatsapp_business_account";

/** The only `change.field` the ingestion reads. */
export const MESSAGES_FIELD = "messages";

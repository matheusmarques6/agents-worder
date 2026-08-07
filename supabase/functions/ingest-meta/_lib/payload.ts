// What the door hands to the database — pure translation, no I/O.
//
// Extracted so the two mappings that matter can be asserted directly: the
// content row an inbound message becomes (including the consent tap), and the
// verdict a status update maps to (including the ones that map to nothing).

import type { InboundMessage, StatusUpdate } from "./schema.ts";

/** `internal.ingest_webhook`'s `p_payload` for a message. Assembled here, never
 * copied blindly from Meta's envelope. */
export function messageContent(message: InboundMessage) {
  return {
    from: `+${message.from}`,
    message: {
      type: message.type,
      text: message.text?.body ?? null,
      // Lifted to the same level as `text` on purpose: whoever reads the row in
      // `public.messages.content` looks for the tap in one place, without
      // knowing Meta's format. `dispatch.consent.recognize` reads exactly this
      // key, and the id travels verbatim — recognition is a lookup against the
      // ids we issued, so a translation here would be a silent unblocking.
      button_reply: message.interactive?.button_reply ?? null,
    },
  };
}

/** The outbox verdict a status update carries, or `null` when it carries none. */
export interface StatusCorrelation {
  /** `biz_opaque_callback_data` — our key, echoed back by the platform. */
  opaqueData: string;
  /** What `internal.correlate_outbox_status` records. */
  status: "sent" | "failed";
  /** The wamid the platform is talking about. */
  wamid: string;
}

/** `null` means "nothing to correlate", and it is not an error:
 *  - without the opaque key there is no row of ours to match — statuses from
 *    other origins (or from before us) go by on purpose;
 *  - a raw `sent` decides nothing; `delivered`/`read`/`failed` do. */
export function statusCorrelation(status: StatusUpdate): StatusCorrelation | null {
  if (!status.biz_opaque_callback_data) return null;
  if (status.status === "sent") return null;

  return {
    opaqueData: status.biz_opaque_callback_data,
    status: status.status === "failed" ? "failed" : "sent",
    wamid: status.id,
  };
}

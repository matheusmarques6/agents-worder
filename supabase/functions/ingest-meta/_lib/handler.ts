// The branch decision — which status code a request earns, and why.
//
// Four outcomes, and each one is a different sentence to Meta:
//   401  the signature does not check out — nothing touched the database;
//   200  signed by Meta but off-format — THEIR error, nothing is ingested, and
//        no redelivery will ever make it parse, so redelivery must stop;
//   500  OUR error (database down, bug) — Meta redelivers, and the ingestion's
//        idempotency makes redelivery safe;
//   200  handled (or deliberately ignored) — stop redelivering.
//
// The axis is not "was it valid" but "would sending it again help": every
// non-200 buys a retry, and a retry is worth buying only when the failure was
// ours and transient.
//
// Everything here is a decision over data; the single side effect is behind
// `IngestPort`, which `index.ts` implements with SQL and a test implements with
// an array.

import {
  ChangeValue,
  type InboundMessage,
  MESSAGES_FIELD,
  WebhookBody,
  WHATSAPP_OBJECT,
} from "./schema.ts";
import { type StatusCorrelation, statusCorrelation } from "./payload.ts";
import { signatureIsValid, SIGNATURE_HEADER } from "./signature.ts";

/** The only thing this module is allowed to do to the world. */
export interface IngestPort {
  ingestMessage(phoneNumberId: string, message: InboundMessage): Promise<void>;
  correlateStatus(correlation: StatusCorrelation): Promise<void>;
}

export interface Deps {
  appSecret: string;
  verifyToken: string;
  port: IngestPort;
  /** Where everything unexpected goes — a 500, an off-format payload, a
   * discarded change. Injected so a test can watch it instead of the terminal;
   * production keeps `console.error`. */
  onError?: (error: unknown) => void;
}

/** Where anything unexpected goes. Production keeps `console.error`; a test
 * hands in a collector so the terminal stays readable. */
function report(deps: Deps, error: unknown): void {
  (deps.onError ?? ((thrown: unknown) => console.error("[ingest-meta]", thrown)))(error);
}

/** What `describeDiscard` needs from a zod failure — structural on purpose, so
 * the branch decision keeps its one dependency on the schema module and does
 * not grow a second one on zod itself. */
interface ParseFailure {
  issues: ReadonlyArray<{ path: ReadonlyArray<string | number>; code: string }>;
}

/** The line a dropped change leaves behind: how much was lost and which fields
 * refused. Paths and codes ONLY — why a change was rejected never needs the
 * content that was rejected, and message bodies do not leave Postgres. */
function describeDiscard(value: unknown, failure: ParseFailure): string {
  const change = (typeof value === "object" && value !== null)
    ? value as { messages?: unknown; statuses?: unknown }
    : {};
  const messages = Array.isArray(change.messages) ? change.messages.length : 0;
  const statuses = Array.isArray(change.statuses) ? change.statuses.length : 0;

  const refused = failure.issues
    .map((issue) => `${issue.path.join(".") || "(root)"}=${issue.code}`)
    .join(", ");

  return `change discarded: ${messages + statuses} entries dropped ` +
    `(${messages} messages, ${statuses} statuses); refused: ${refused}`;
}

/** Meta's subscription handshake. */
export function handleVerification(url: URL, verifyToken: string): Response {
  const mode = url.searchParams.get("hub.mode");
  const token = url.searchParams.get("hub.verify_token");
  const challenge = url.searchParams.get("hub.challenge");

  if (mode !== "subscribe" || token !== verifyToken || !challenge) {
    return new Response("forbidden", { status: 403 });
  }
  return new Response(challenge, { status: 200 });
}

export async function handleEvents(request: Request, deps: Deps): Promise<Response> {
  const rawBody = await request.text();

  if (!(await signatureIsValid(rawBody, request.headers.get(SIGNATURE_HEADER), deps.appSecret))) {
    return new Response("invalid signature", { status: 401 });
  }

  let body: WebhookBody;
  try {
    body = WebhookBody.parse(JSON.parse(rawBody));
  } catch (error) {
    // Signed by Meta but off-format. Nothing is ingested — "use what parsed" is
    // still forbidden — but the answer is 200 and not 400: the Cloud API retries
    // EVERY non-200 with backoff for days, and an envelope that does not parse
    // does not parse on the second delivery either. A 400 here buys nothing and
    // costs a redelivery storm against a subscription Meta eventually disables.
    // Loud on OUR side, terminal on theirs.
    report(deps, error);
    return new Response(JSON.stringify({ ignored: true, reason: "malformed" }), { status: 200 });
  }

  if (body.object !== WHATSAPP_OBJECT) {
    return new Response(JSON.stringify({ ignored: true }), { status: 200 });
  }

  for (const entry of body.entry ?? []) {
    for (const change of entry.changes ?? []) {
      if (change.field !== MESSAGES_FIELD) continue;

      const parsed = ChangeValue.safeParse(change.value);
      if (!parsed.success) {
        // Strict: what does not validate does not enter — and one invalid entry
        // fails its whole change, because half a change ingested is worse than
        // none. But dropping it quietly would make the loss invisible, which is
        // the failure mode this product hunts hardest: the report says how many
        // entries went with it and which fields refused, so the gap in the trail
        // has a line explaining itself.
        report(deps, new Error(describeDiscard(change.value, parsed.error)));
        continue;
      }

      const { metadata, messages, statuses } = parsed.data;
      for (const message of messages ?? []) {
        await deps.port.ingestMessage(metadata.phone_number_id, message);
      }
      for (const status of statuses ?? []) {
        const correlation = statusCorrelation(status);
        if (correlation) await deps.port.correlateStatus(correlation);
      }
    }
  }

  return new Response(JSON.stringify({ received: true }), { status: 200 });
}

/** The whole door, minus the HTTP server and the database handle. */
export async function handleRequest(request: Request, deps: Deps): Promise<Response> {
  const url = new URL(request.url);

  if (request.method === "GET") return handleVerification(url, deps.verifyToken);
  if (request.method === "POST") {
    try {
      return await handleEvents(request, deps);
    } catch (error) {
      // OUR error. 500 makes Meta redeliver, and the ingestion's idempotency
      // makes the redelivery safe. It is the only place where "try again" is
      // the right answer to the sender.
      report(deps, error);
      return new Response("internal error", { status: 500 });
    }
  }
  return new Response("method not allowed", { status: 405 });
}

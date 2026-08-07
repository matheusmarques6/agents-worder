// The first trust boundary — HMAC-SHA256 over the RAW body.
//
// Extracted from `index.ts` unchanged: same prefix rule, same hex digest, same
// constant-time comparison. It lives apart from the HTTP seam for one reason —
// a gate nobody can call in a test is a gate nobody proves.

/** What the comparison actually needs from a string. Declared so a test can
 * hand it a counting probe and prove the loop never exits early: `string`
 * satisfies this structurally, so production still passes plain strings. */
export interface HexLike {
  readonly length: number;
  charCodeAt(index: number): number;
}

export const SIGNATURE_HEADER = "x-hub-signature-256";

const SIGNATURE_PREFIX = "sha256=";

const encoder = new TextEncoder();

/** Lowercase hex HMAC-SHA256 of `body` under `secret` (both UTF-8). */
export async function hmacHex(secret: string, body: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const digest = await crypto.subtle.sign("HMAC", key, encoder.encode(body));
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

/** Constant-time equality: a comparator that gives up at the first differing
 * byte leaks, byte by byte, how much of the signature the attacker got right.
 * Length is compared first — hex digests of a fixed hash always share it, so
 * a mismatch there is already public information. */
export function timingSafeEqual(given: HexLike, expected: HexLike): boolean {
  if (given.length !== expected.length) return false;

  let diff = 0;
  for (let index = 0; index < expected.length; index++) {
    diff |= given.charCodeAt(index) ^ expected.charCodeAt(index);
  }
  return diff === 0;
}

/** Absent header, wrong prefix or wrong digest are all one answer: false.
 * worder1 verified and proceeded anyway; here invalid means the request never
 * reaches the database. */
export async function signatureIsValid(
  rawBody: string,
  header: string | null,
  appSecret: string,
): Promise<boolean> {
  if (!header || !header.startsWith(SIGNATURE_PREFIX)) return false;

  const expected = await hmacHex(appSecret, rawBody);
  return timingSafeEqual(header.slice(SIGNATURE_PREFIX.length), expected);
}

/** The header value for a body — production never signs, but the tests and any
 * local replay tool need exactly the format the verifier accepts. */
export async function signatureHeaderFor(
  rawBody: string,
  appSecret: string,
): Promise<string> {
  return `${SIGNATURE_PREFIX}${await hmacHex(appSecret, rawBody)}`;
}

// The first boundary, under test: a request that is not Meta's never reaches
// the database, and a request that is Meta's is not rejected by accident.

import { assert, assertEquals, assertFalse } from "jsr:@std/assert@1.0.0";

import {
  type HexLike,
  hmacHex,
  signatureHeaderFor,
  signatureIsValid,
  timingSafeEqual,
} from "./signature.ts";

const SECRET = "app-secret-do-tenant";
const BODY = JSON.stringify({ object: "whatsapp_business_account", entry: [] });

Deno.test("hmacHex matches the RFC 4231 vector", async () => {
  // Case 2 of RFC 4231: a UTF-8 string key and ASCII data, which is exactly the
  // shape Meta signs with. A known answer, so the whole suite is not just this
  // implementation agreeing with itself.
  assertEquals(
    await hmacHex("Jefe", "what do ya want for nothing?"),
    "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843",
  );
});

Deno.test("a signature Meta produced is accepted", async () => {
  const header = await signatureHeaderFor(BODY, SECRET);

  assert(header.startsWith("sha256="));
  assert(await signatureIsValid(BODY, header, SECRET));
});

Deno.test("a signature under another secret is rejected", async () => {
  const header = await signatureHeaderFor(BODY, "outro-segredo");

  assertFalse(await signatureIsValid(BODY, header, SECRET));
});

Deno.test("a body tampered with after signing is rejected", async () => {
  const header = await signatureHeaderFor(BODY, SECRET);
  const tampered = BODY.replace("whatsapp_business_account", "whatsapp_business_accounX");

  assertEquals(tampered.length, BODY.length); // same length: only the bytes moved
  assertFalse(await signatureIsValid(tampered, header, SECRET));
});

Deno.test("an absent header is rejected", async () => {
  assertFalse(await signatureIsValid(BODY, null, SECRET));
  assertFalse(await signatureIsValid(BODY, "", SECRET));
});

Deno.test("a header without the sha256= prefix is rejected", async () => {
  const digest = await hmacHex(SECRET, BODY);

  assertFalse(await signatureIsValid(BODY, digest, SECRET)); // bare digest
  assertFalse(await signatureIsValid(BODY, `sha1=${digest}`, SECRET));
  assertFalse(await signatureIsValid(BODY, `SHA256=${digest}`, SECRET));
});

Deno.test("a truncated or padded digest is rejected", async () => {
  const digest = await hmacHex(SECRET, BODY);

  assertFalse(await signatureIsValid(BODY, `sha256=${digest.slice(0, -1)}`, SECRET));
  assertFalse(await signatureIsValid(BODY, `sha256=${digest}0`, SECRET));
  assertFalse(await signatureIsValid(BODY, "sha256=", SECRET));
});

Deno.test("the digest is compared as lowercase hex — the case Meta sends", async () => {
  // Characterisation, not preference: the header Meta sends is lowercase, and
  // accepting both cases would mean normalising attacker-controlled input
  // before a constant-time compare.
  const digest = await hmacHex(SECRET, BODY);

  assertFalse(await signatureIsValid(BODY, `sha256=${digest.toUpperCase()}`, SECRET));
});

Deno.test("timingSafeEqual reads every character even when the first differs", () => {
  const expected = "abcdef";
  let reads = 0;
  const probe: HexLike = {
    length: expected.length,
    charCodeAt(index: number) {
      reads++;
      return "zbcdef".charCodeAt(index);
    },
  };

  assertFalse(timingSafeEqual(probe, expected));
  // A comparator that gave up at the first differing character would have read
  // once. Reading all six is the whole point: the number of comparisons must
  // not tell the attacker how many characters they got right.
  assertEquals(reads, expected.length);
});

Deno.test("timingSafeEqual agrees with equality on same-length input", () => {
  assert(timingSafeEqual("abcdef", "abcdef"));
  assertFalse(timingSafeEqual("abcdez", "abcdef")); // differs at the last position
  assertFalse(timingSafeEqual("abcde", "abcdef")); // shorter
  assertFalse(timingSafeEqual("abcdefg", "abcdef")); // longer
});

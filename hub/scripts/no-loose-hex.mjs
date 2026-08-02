// Fitness function — colour lives in the token layer, nowhere else.
//
// A hex in a component is how a design system dies: it works, it ships, and
// six months later the brand orange exists in four slightly different values
// that no theme switch can reach. The token contract (e2e/design-tokens.spec.ts)
// says which colours exist; this says nobody may invent one.
//
// Deliberately not an ESLint rule: it has to cover CSS as well as TSX, and a
// twenty-line script that reads its own failure message out loud is easier to
// trust than a plugin.

import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HUB = resolve(dirname(fileURLToPath(import.meta.url)), "..");

// Where product code lives. `e2e/` is out of scope on purpose: the token
// contract asserts the palette, so it must be allowed to spell it out.
const ROOTS = ["app", "components"];

// The one file allowed to hold a colour. It is the transcription of the design
// system, and the only place a new colour can be introduced.
const TOKEN_FILE = "app/globals.css";

const EXTENSIONS = [".ts", ".tsx", ".css"];

// 3, 4, 6 or 8 digits — #fff, #fff8, #F97316, #F9731680 — and nothing else, so
// an id selector or a URL fragment is not a colour.
const HEX = /(?<![\w#])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})(?![\w])/g;

function* sourceFiles(directory) {
  let entries;
  try {
    entries = readdirSync(directory);
  } catch {
    return; // a root that does not exist yet — components/ before the first one
  }

  for (const entry of entries) {
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) {
      yield* sourceFiles(path);
    } else if (EXTENSIONS.some((extension) => entry.endsWith(extension))) {
      yield path;
    }
  }
}

const findings = [];

for (const root of ROOTS) {
  for (const path of sourceFiles(join(HUB, root))) {
    const relativePath = relative(HUB, path).split("\\").join("/");
    if (relativePath === TOKEN_FILE) continue;

    readFileSync(path, "utf8")
      .split("\n")
      .forEach((line, index) => {
        for (const match of line.matchAll(HEX)) {
          findings.push(`${relativePath}:${index + 1}  ${match[0]}`);
        }
      });
  }
}

if (findings.length > 0) {
  console.error(
    `\nColour outside the token layer (${findings.length}):\n\n${findings
      .map((finding) => `  ${finding}`)
      .join("\n")}\n\n` +
      `Use a token from ${TOKEN_FILE} — bg-surface-raised, text-fg-muted, border-brand-500.\n` +
      `If the colour genuinely does not exist yet, it is a decision for the design\n` +
      `system first (Claude Design), then a token here, then a utility in the component.\n`,
  );
  process.exit(1);
}

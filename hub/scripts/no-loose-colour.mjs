// Fitness function — colour and glass live in the token layer, nowhere else.
//
// A hex in a component is how a design system dies: it works, it ships, and six
// months later the brand orange exists in four slightly different values that no
// theme switch can reach. The token contract (e2e/design-tokens.spec.ts) says
// which colours exist; this says nobody may invent one.
//
// It checks two things:
//
//   1. No colour literal outside the token file — hex, rgb(), rgba(), hsl(),
//      hsla(), oklch(), oklab(), color(). Hex alone was enough until E0-14,
//      when translucent surfaces arrived and rgba() became the natural way to
//      write one.
//
//   2. No backdrop blur outside the glass primitive. Stacking two blurred
//      layers is precisely what `Glass` exists to prevent, and a stray
//      `backdrop-blur-xl` utility on a div reintroduces the bug from outside
//      the component that guards against it.
//
// Deliberately not an ESLint rule: it has to cover CSS as well as TSX, and a
// script that reads its own failure message out loud is easier to trust than a
// plugin.

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

// The glass recipe is CSS, so it lives in the token file too; the component
// only decides which level applies. Neither is listed for colour — the token
// file is already exempt, and glass.tsx holds no colour at all.
const BLUR_ALLOWED = new Set([TOKEN_FILE]);

const EXTENSIONS = [".ts", ".tsx", ".css"];

const RULES = [
  {
    what: "colour",
    // 3, 4, 6 or 8 digits — #fff, #fff8, #F97316, #F9731680 — plus every
    // functional notation CSS accepts today. `color-mix` is allowed because it
    // composes tokens rather than inventing one.
    //
    // In TypeScript the hex must sit in a colour-ish context: opened by a
    // quote, a parenthesis or a property colon. An order number reads as hex
    // — `pedido #4821` is four valid hex digits — and flagging product copy
    // teaches people to distrust the check. In CSS the same restriction would
    // create false negatives (`border: 1px solid #fff` follows a space), so
    // there the bare form still counts.
    pattern: (path) =>
      path.endsWith(".css")
        ? /(?<![\w#])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})(?![\w])|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\s*\(/g
        : /(?<=["'`(]|:\s{0,4})#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})(?![\w])|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\s*\(/g,
    allowed: (path) => path === TOKEN_FILE,
    fix:
      `Use a token from ${TOKEN_FILE} — bg-surface-raised, text-fg-muted, border-brand-500.\n` +
      `If the colour genuinely does not exist yet, it is a decision for the design\n` +
      `system first (Claude Design), then a token here, then a utility in the component.`,
  },
  {
    what: "backdrop blur",
    pattern: () => /\bbackdrop-(?:filter|blur)\b/g,
    allowed: (path) => BLUR_ALLOWED.has(path),
    fix:
      `Use <Glass level="chrome|card|overlay"> from components/glass.tsx.\n` +
      `Glass stacks exactly once (design §03) and the component is what enforces it —\n` +
      `a blur applied by hand is outside that guarantee.`,
  },
];

// Prose is not code. A comment explaining WHY a blur belongs to the glass
// primitive, or naming the hex it replaced, is documentation — flagging it
// teaches people to stop writing comments, which is the opposite of the point.
// The same lesson as the AST detectors in the runtime (E0-06); here a
// line-oriented strip is enough, because the goal is to remove text before
// searching it, and removing too much can only produce a false negative in a
// line that mixes a URL and a colour.
function withoutComments(source) {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, (block) => block.replace(/[^\n]/g, " "))
    .replace(/(^|[^:])\/\/.*$/gm, "$1");
}

function* sourceFiles(directory) {
  let entries;
  try {
    entries = readdirSync(directory);
  } catch {
    return; // a root that does not exist yet
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

let failed = false;

for (const rule of RULES) {
  const findings = [];

  for (const root of ROOTS) {
    for (const path of sourceFiles(join(HUB, root))) {
      const relativePath = relative(HUB, path).split("\\").join("/");
      if (rule.allowed(relativePath)) continue;

      withoutComments(readFileSync(path, "utf8"))
        .split("\n")
        .forEach((line, index) => {
          for (const match of line.matchAll(rule.pattern(relativePath))) {
            findings.push(`${relativePath}:${index + 1}  ${match[0]}`);
          }
        });
    }
  }

  if (findings.length > 0) {
    failed = true;
    console.error(
      `\n${rule.what} outside the token layer (${findings.length}):\n\n` +
        `${findings.map((finding) => `  ${finding}`).join("\n")}\n\n${rule.fix}\n`,
    );
  }
}

if (failed) process.exit(1);

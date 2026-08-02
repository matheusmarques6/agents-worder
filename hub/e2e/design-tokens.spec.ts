import { expect, test } from "@playwright/test";

// E0-13 — the token contract.
//
// Every value here was read from `Agents Worder - Design System.dc.html` in
// Claude Design (sections 01–04), which is the contract. This journey is what
// makes the token layer a contract in code too: rename a token, drop one, or
// quietly change a hex, and this fails by name.
//
// It asserts the CUSTOM PROPERTIES, not the utilities. Utilities come and go as
// components get built; the token names are what every screen from E4 onwards
// will be written against, and what the "no loose hex" lint points people at.

type Tokens = Record<string, string>;

async function tokensOf(page: import("@playwright/test").Page, names: string[]): Promise<Tokens> {
  return page.evaluate((wanted) => {
    const style = getComputedStyle(document.documentElement);
    return Object.fromEntries(wanted.map((name) => [name, style.getPropertyValue(name).trim()]));
  }, names);
}

// Expected values are written exactly as the design writes them — `#FFFFFF`,
// `-0.035em` — while the production build ships the shortest equivalent
// spelling: lowercase, `#fff`, `-.035em`. Those are serialisations of the same
// value, so they are canonicalised on both sides rather than transcribed in
// minified form, which would make the contract stop reading like its source.
function canonical(value: string): string {
  return value
    .toLowerCase()
    .replace(/^#([0-9a-f])([0-9a-f])([0-9a-f])$/, "#$1$1$2$2$3$3")
    .replace(/(^|[\s(,])(-?)\.(\d)/g, "$1$20.$3");
}

function normalise(tokens: Tokens): Tokens {
  return Object.fromEntries(
    Object.entries(tokens).map(([name, value]) => [name, canonical(value)]),
  );
}

async function expectTokens(page: import("@playwright/test").Page, expected: Tokens) {
  expect(normalise(await tokensOf(page, Object.keys(expected)))).toEqual(normalise(expected));
}

// 01 · Cor — the brand ramp. Orange is action, state and data; never a page
// background. Seven steps is what the design defines, so seven is what exists:
// an eighth would be someone inventing a colour.
const BRAND = {
  "--color-brand-50": "#FFF7ED",
  "--color-brand-100": "#FFEDD5",
  "--color-brand-200": "#FED7AA",
  "--color-brand-300": "#FDBA74",
  "--color-brand-500": "#F97316",
  "--color-brand-600": "#EA580C",
  "--color-brand-900": "#7C2D12",
};

// Used only for state, never for decoration.
const SEMANTIC = {
  "--color-success": "#34D399",
  "--color-warning": "#FACC15",
  "--color-danger": "#F43F5E",
};

const DARK_SURFACES = {
  "--color-surface": "#08090C",
  "--color-surface-raised": "#0F1014",
  "--color-surface-solid": "#1A1B20",
  "--color-fg": "#F4F4F5",
  "--color-fg-muted": "#A9AAB2",
  "--color-fg-subtle": "#75767E",
  "--color-fg-disabled": "#5F6067",
};

const LIGHT_SURFACES = {
  "--color-surface": "#F3F2F0",
  "--color-surface-raised": "#FFFFFF",
  "--color-surface-solid": "#E9E7E3",
  "--color-fg": "#17181C",
  "--color-fg-muted": "#5A5B62",
  "--color-fg-subtle": "#7A7B82",
  "--color-fg-disabled": "#A8A9AF",
};

// 02 · Tipografia — Geist for the interface, Geist Mono for numbers, IDs,
// labels and code.
const TYPE = {
  "--text-display": "44px",
  "--text-metric": "34px",
  "--text-title": "19px",
  "--text-card": "14px",
  "--text-body": "13.5px",
  "--text-small": "12px",
  "--text-label": "10px",
};

const TYPE_DETAIL = {
  "--text-display--line-height": "1.05",
  "--text-display--letter-spacing": "-0.035em",
  "--text-metric--letter-spacing": "-0.03em",
  "--text-title--letter-spacing": "-0.015em",
  "--text-body--line-height": "1.6",
  "--text-label--letter-spacing": "0.14em",
};

// 03 · Liquid Glass — only the blurs live here. The surfaces, borders and
// highlights of the three levels arrive with the primitive in E0-14, where
// there is something to assert them against.
const BLUR = {
  "--blur-chrome": "28px",
  "--blur-card": "24px",
  "--blur-overlay": "40px",
};

// 04 · Raio — named by use, exactly as the design states it: 8 chips and small
// inputs, 12 buttons and nav, 18–22 cards and overlays, full for pills.
const RADIUS = {
  "--radius-chip": "8px",
  "--radius-control": "12px",
  "--radius-card": "18px",
  "--radius-chrome": "20px",
  "--radius-overlay": "22px",
  "--radius-pill": "9999px",
};

// 04 · Espaçamento (base 2) and the app measurements.
const SPACE = {
  "--spacing": "2px",
  "--spacing-chip": "4px",
  "--spacing-item": "8px",
  "--spacing-cards": "14px",
  "--spacing-card": "18px",
  "--spacing-section": "32px",
  "--spacing-shell": "14px",
  "--spacing-touch": "44px",
  "--spacing-sidebar": "242px",
  "--spacing-aside": "340px",
  "--spacing-reading": "680px",
};

test.describe("design tokens", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
  });

  test("exposes the brand ramp and the semantic colours", async ({ page }) => {
    await expectTokens(page, { ...BRAND, ...SEMANTIC });
  });

  test("is dark by default", async ({ page }) => {
    await expectTokens(page, DARK_SURFACES);
  });

  test("switches surfaces and text when the light theme is selected", async ({ page }) => {
    await page.evaluate(() => document.documentElement.setAttribute("data-theme", "light"));

    await expectTokens(page, LIGHT_SURFACES);
  });

  test("keeps the brand ramp identical in both themes", async ({ page }) => {
    // The ramp is the brand, not a surface: a light theme that shifts it would
    // be a second brand nobody approved.
    const dark = normalise(await tokensOf(page, Object.keys(BRAND)));
    // Without this, the test also passes when NEITHER theme defines the ramp —
    // two nothings compare equal.
    expect(dark).toEqual(normalise(BRAND));

    await page.evaluate(() => document.documentElement.setAttribute("data-theme", "light"));

    expect(normalise(await tokensOf(page, Object.keys(BRAND)))).toEqual(dark);
  });

  test("exposes the named type scale", async ({ page }) => {
    await expectTokens(page, { ...TYPE, ...TYPE_DETAIL });
  });

  test("exposes the three glass blurs", async ({ page }) => {
    await expectTokens(page, BLUR);
  });

  test("exposes radii named by use", async ({ page }) => {
    await expectTokens(page, RADIUS);
  });

  test("exposes the spacing base and the app measurements", async ({ page }) => {
    await expectTokens(page, SPACE);
  });

  test("binds the interface and mono families to the pinned Geist files", async ({ page }) => {
    const fonts = await tokensOf(page, ["--font-sans", "--font-mono"]);

    // Asserted as "not empty and different from each other" rather than by
    // value: the value is a hashed variable emitted by the `geist` package and
    // changes with every version bump, which would make this a test about
    // Next.js instead of about the design system.
    expect(fonts["--font-sans"]).not.toBe("");
    expect(fonts["--font-mono"]).not.toBe("");
    expect(fonts["--font-sans"]).not.toBe(fonts["--font-mono"]);
  });
});

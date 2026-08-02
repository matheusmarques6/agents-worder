import { expect, test } from "@playwright/test";

import { canonicalAll } from "./support/css";

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

// Values are written here exactly as the design writes them; the build ships
// the shortest equivalent spelling. See e2e/support/css.ts for why comparing
// canonical forms is the honest way to assert that.
async function expectTokens(page: import("@playwright/test").Page, expected: Tokens) {
  expect(canonicalAll(await tokensOf(page, Object.keys(expected)))).toEqual(canonicalAll(expected));
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

// 03 · Liquid Glass — the three levels, blur and surface. The recipe is
// asserted end-to-end in glass.spec.ts; what is asserted here is that the
// values exist as tokens, so a component can never spell one out.
const BLUR = {
  "--blur-chrome": "28px",
  "--blur-card": "24px",
  "--blur-overlay": "40px",
};

const GLASS = {
  "--color-glass-chrome-from": "rgba(255, 255, 255, 0.075)",
  "--color-glass-chrome-to": "rgba(255, 255, 255, 0.03)",
  "--color-glass-chrome-border": "rgba(255, 255, 255, 0.10)",
  "--color-glass-card-from": "rgba(255, 255, 255, 0.06)",
  "--color-glass-card-to": "rgba(255, 255, 255, 0.022)",
  "--color-glass-card-border": "rgba(255, 255, 255, 0.09)",
  "--color-glass-overlay-from": "rgba(255, 255, 255, 0.10)",
  "--color-glass-overlay-to": "rgba(255, 255, 255, 0.045)",
  "--color-glass-overlay-border": "rgba(255, 255, 255, 0.15)",
  // The surface a glass falls back to when it finds itself inside another one.
  "--color-glass-nested": "rgba(255, 255, 255, 0.045)",
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

// 05 · 06 — the controls. Dimensions only: the colour of each variant is
// asserted where it can be seen, in controls.spec.ts and in the baseline. What
// matters here is that the NAMES survive, because a screen in E4 that says
// `size="lg"` is trusting these to still mean 48px.
const CONTROL = {
  "--spacing-control-sm": "30px",
  "--spacing-control-md": "38px",
  "--spacing-control-lg": "48px",
  "--radius-control-sm": "9px",
  "--radius-control-lg": "14px",
  "--text-control-sm": "12px",
  "--text-control-md": "13px",
  "--text-control-lg": "14.5px",
  "--radius-choice": "13px",
  "--spacing-toggle-track-w": "44px",
  "--spacing-toggle-track-h": "26px",
  "--spacing-toggle-knob": "20px",
  "--spacing-spinner": "11px",
  "--text-field": "13.5px",
  "--text-field-label": "12.5px",
  "--text-field-help": "11.5px",
  "--spacing-textarea": "58px",
};

// 07 — status and feedback. The five status borders are here because they are
// what makes two states distinguishable at a glance; feedback.spec.ts asserts
// that no two of them are equal, and this asserts what each one is.
const FEEDBACK = {
  "--radius-badge": "5px",
  "--radius-skeleton": "6px",
  "--radius-feedback": "16px",
  "--text-badge": "11.5px",
  "--text-tech": "10.5px",
  "--color-status-active-border": "rgba(249, 115, 22, 0.30)",
  "--color-status-paused-border": "rgba(255, 255, 255, 0.12)",
  "--color-status-onboarding-border": "rgba(250, 204, 21, 0.26)",
  "--color-status-shadow-border": "rgba(249, 115, 22, 0.5)",
  "--color-status-cancelled-border": "rgba(244, 63, 94, 0.28)",
};

// The design defines mobile as BELOW 860px; Tailwind breakpoints are
// min-width, so the token is the desktop side of that same line. It was in the
// stylesheet from E0-13 but outside this contract, which meant renaming it
// broke nothing — the exact hole a token contract exists to close.
const BREAKPOINT = {
  "--breakpoint-desk": "860px",
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
    const dark = canonicalAll(await tokensOf(page, Object.keys(BRAND)));
    // Without this, the test also passes when NEITHER theme defines the ramp —
    // two nothings compare equal.
    expect(dark).toEqual(canonicalAll(BRAND));

    await page.evaluate(() => document.documentElement.setAttribute("data-theme", "light"));

    expect(canonicalAll(await tokensOf(page, Object.keys(BRAND)))).toEqual(dark);
  });

  test("exposes the named type scale", async ({ page }) => {
    await expectTokens(page, { ...TYPE, ...TYPE_DETAIL });
  });

  test("exposes the three glass blurs", async ({ page }) => {
    await expectTokens(page, BLUR);
  });

  test("exposes the surface of each glass level and the nested fallback", async ({ page }) => {
    await expectTokens(page, GLASS);
  });

  test("exposes the dimensions every control is built from", async ({ page }) => {
    await expectTokens(page, CONTROL);
  });

  test("exposes the feedback tokens, including a distinct border per status", async ({ page }) => {
    await expectTokens(page, FEEDBACK);
  });

  test("exposes the desktop breakpoint", async ({ page }) => {
    await expectTokens(page, BREAKPOINT);
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

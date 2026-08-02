import { expect, test } from "@playwright/test";

// E0-17 — visual regression, the part that compares pixels.
//
// Everything asserted elsewhere in this suite is structure and computed style:
// exact, cheap, and blind to the thing a design system is actually for. This
// file is the other half — it compares what the component LOOKS like against a
// baseline produced by CI.
//
// Two decisions carry the whole harness:
//
//   1. Capture per SECTION, never the whole page. The showcase grows by four
//      lots in E0-15; a full-page baseline would be invalidated by every lot
//      and would make each PR rewrite the evidence of the previous ones. Per
//      section, a lot only ever creates its own.
//
//   2. Baselines come from CI and only from CI (playwright.config.ts:
//      `ignoreSnapshots` when CI is unset). A screenshot taken on a developer
//      machine — different font rasteriser, different GPU compositing of the
//      blur — would silently become the contract everyone else is compared
//      against.
//
// Both themes are captured for glass, because glass is what changes most
// between them: dark is white at 6% over near-black, light is white at 88%
// over warm beige.

const THEMES = ["dark", "light"] as const;

test.describe("visual regression", () => {
  for (const theme of THEMES) {
    test(`section 03 · liquid glass · ${theme}`, async ({ page }) => {
      await page.goto("/design");
      await page.evaluate(
        (selected) => document.documentElement.setAttribute("data-theme", selected),
        theme,
      );

      // The section sits on the opaque stage the showcase draws behind every
      // component (R1): a blur photographed over a gradient is the least
      // deterministic pixel in the system.
      await expect(page.getByTestId("showcase-section-03")).toHaveScreenshot(
        `secao-03-glass-${theme}.png`,
      );
    });
  }
});

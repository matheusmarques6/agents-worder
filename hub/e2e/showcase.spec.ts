import { expect, test } from "@playwright/test";

import { canonical } from "./support/css";

// E0-16 — the showcase shell.
//
// The showcase is where fidelity to the design is checked, so its own
// structure has to mirror the design's: the same thirteen sections, in the
// same order, under the same numbers. A showcase that drifts from the document
// it mirrors stops being evidence and becomes decoration.
//
// The four component lots of E0-15 land in sections 05–11. Everything asserted
// here has to survive that without being edited.

const SECTIONS = [
  "01",
  "02",
  "03",
  "04",
  "05",
  "06",
  "07",
  "08",
  "09",
  "10",
  "11",
  "12",
  "13",
];

const DARK_SURFACE = "#08090C";
const LIGHT_SURFACE = "#F3F2F0";

async function bodySurface(page: import("@playwright/test").Page): Promise<string> {
  return canonical(
    await page.evaluate(() => getComputedStyle(document.body).backgroundColor),
  );
}

test.describe("design showcase", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/design");
  });

  test("mirrors the thirteen sections of the design, in order", async ({ page }) => {
    const ids = await page
      .locator("[data-testid^='showcase-section-']")
      .evaluateAll((nodes) =>
        nodes.map((node) => (node as HTMLElement).dataset.testid?.replace("showcase-section-", "")),
      );

    expect(ids).toEqual(SECTIONS);
  });

  test("every section is reachable from the navigation", async ({ page }) => {
    // A showcase nobody can navigate is a showcase nobody reviews.
    const targets = await page
      .getByTestId("showcase-nav")
      .locator("a")
      .evaluateAll((nodes) => nodes.map((node) => (node as HTMLAnchorElement).hash));

    expect(targets).toEqual(SECTIONS.map((section) => `#secao-${section}`));
  });

  test("the theme switch changes the theme, not just a button state", async ({ page }) => {
    // Asserted on the computed surface rather than on the attribute: setting
    // data-theme and having nothing react to it is the exact failure this
    // catches, and the reviewer of a component PR depends on this control to
    // see both themes without opening a console.
    expect(await bodySurface(page)).toBe(canonical(DARK_SURFACE));

    await page.getByTestId("theme-switch").click();
    expect(await bodySurface(page)).toBe(canonical(LIGHT_SURFACE));

    await page.getByTestId("theme-switch").click();
    expect(await bodySurface(page)).toBe(canonical(DARK_SURFACE));
  });

  test("dark is where it starts", async ({ page }) => {
    // The product's face. A showcase that opened in light would quietly make
    // light the reference for every review.
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  });

  test("does not scroll sideways", async ({ page }) => {
    // Same cheap marker as the home journey (E0-09), now on a dense page with
    // a navigation column — which is where a 390px viewport earns its keep.
    const overflows = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
    );

    expect(overflows).toBe(false);
  });
});

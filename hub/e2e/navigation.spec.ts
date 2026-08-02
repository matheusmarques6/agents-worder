import { expect, test, type Locator } from "@playwright/test";

import { canonical } from "./support/css";

// E0-15 · L3 — navigation and data.
//
// The first lot whose behaviour differs by viewport, and therefore the first
// time the breakpoint is asserted rather than assumed. Both navigation blocks
// are always rendered; CSS decides which one is on screen. That is deliberate:
// a JS-switched layout would make this test pass while proving nothing about
// the stylesheet a merchant actually gets.

async function styleOf(element: Locator, property: string): Promise<string> {
  return canonical(
    await element.evaluate(
      (node, name) => getComputedStyle(node as Element).getPropertyValue(name),
      property,
    ),
  );
}

test.describe("the breakpoint decides which navigation exists", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/design");
  });

  test("desktop shows the sidebar and hides the tab bar", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "this is the desktop half of the pair");

    await expect(page.getByTestId("sidebar")).toBeVisible();
    await expect(page.getByTestId("tabbar")).toBeHidden();
  });

  test("mobile shows the tab bar and hides the sidebar", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "this is the mobile half of the pair");

    // The exact inverse. Asserting only one half would pass on a layout that
    // shows both, which on a 390px screen is the sidebar eating the content.
    await expect(page.getByTestId("tabbar")).toBeVisible();
    await expect(page.getByTestId("sidebar")).toBeHidden();
  });

  test("the sidebar is exactly the width the design measures", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "the sidebar does not exist on a phone");

    expect(await styleOf(page.getByTestId("sidebar"), "width")).toBe("242px");
  });

  test("the tab bar is chrome glass with reachable destinations", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "the tab bar does not exist on a desktop");

    expect(await styleOf(page.getByTestId("tabbar"), "backdrop-filter")).toBe("blur(28px)");

    for (const destination of ["home", "inbox", "agente", "mais"]) {
      const box = await page.getByTestId(`tabbar-${destination}`).boundingBox();
      expect(box?.height, `${destination} is below the touch target`).toBeGreaterThanOrEqual(44);
    }
  });
});

test.describe("one orange per block", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/design");
  });

  // Princípio 01, in its testable form: the current destination is the single
  // branded element of its block. Two of them and neither reads as "you are
  // here".
  for (const block of ["sidebar", "tabbar"]) {
    test(`${block} marks exactly one destination as current`, async ({ page }) => {
      await expect(page.getByTestId(block).locator("[aria-current='page']")).toHaveCount(1);
    });
  }
});

test.describe("data table", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/design");
  });

  test("numbers are typeset in mono", async ({ page }) => {
    // Princípio 03. A column of durations in a proportional face cannot be
    // compared down the page, which is the only reason the column exists.
    const family = await styleOf(page.getByTestId("table-cell-elapsed"), "font-family");

    expect(family).toContain("mono");
  });

  test("the status column reuses the badge from L2", async ({ page }) => {
    // Composition across lots again: a second way of drawing a status is a
    // second vocabulary for the same five values.
    await expect(page.getByTestId("table-row-marina").locator("[data-status]")).toHaveCount(1);
  });

  test("the current page is announced", async ({ page }) => {
    await expect(page.getByTestId("pagination").locator("[aria-current='page']")).toHaveCount(1);
  });
});

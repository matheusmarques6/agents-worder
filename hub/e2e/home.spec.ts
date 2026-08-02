import { expect, test } from "@playwright/test";

// E0-09 — the first journey. It carries no product behaviour: what it proves is
// that the E2E level runs at all, in BOTH viewports, against a real server
// build. Everything asserted here is a marker chosen to survive redesign — the
// home will be rebuilt on the design system in T3 and this journey must not
// need editing for that.

test.describe("hub home", () => {
  test("renders the application shell", async ({ page }) => {
    await page.goto("/");

    await expect(page).toHaveTitle(/Agents Worder/);
    await expect(page.getByTestId("hub-home")).toBeVisible();
  });

  test("declares Brazilian Portuguese as the document language", async ({ page }) => {
    await page.goto("/");

    // The whole UI copy is PT-BR; the wrong lang attribute breaks screen
    // readers and hyphenation before anyone notices visually.
    await expect(page.locator("html")).toHaveAttribute("lang", "pt-BR");
  });

  test("does not scroll sideways", async ({ page }) => {
    await page.goto("/");

    // Cheap, and it is the regression that a 390px viewport catches and a
    // 1440px one never does.
    const overflows = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
    );
    expect(overflows).toBe(false);
  });
});

import { expect, test, type Locator } from "@playwright/test";

import { canonical } from "./support/css";

// E0-15 · L4 — overlays, and the rule this whole lot exists for.
//
// Glass never stacks, and `Glass` enforces that through React context. But a
// modal opened from inside a card is NOT stacked on it: it floats over the
// page, and its backdrop-filter samples the page, not the card. The context
// travels anyway — through portals, through the top layer, through everything
// — so without an explicit reset the modal would declare itself nested and
// give up the blur that makes it an overlay.
//
// The two halves of the rule are asserted side by side, from inside the SAME
// card, because either one alone can be satisfied by a component that simply
// turned the rule off.

async function styleOf(element: Locator, property: string): Promise<string> {
  return canonical(
    await element.evaluate(
      (node, name) => getComputedStyle(node as Element).getPropertyValue(name),
      property,
    ),
  );
}

test.describe("the overlay boundary", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/design");
  });

  test("a modal opened from inside a card keeps its blur", async ({ page }) => {
    await page.getByTestId("modal-trigger").click();

    const panel = page.getByTestId("modal-panel");

    await expect(panel).toBeVisible();
    expect(await styleOf(panel, "backdrop-filter")).toBe("blur(40px)");
  });

  test("a plain glass in the same card still loses its blur", async ({ page }) => {
    // The other half. If this ever reads a blur, the fix for the modal was
    // "turn the rule off" rather than "mark the boundary".
    expect(await styleOf(page.getByTestId("overlay-nested-glass"), "backdrop-filter")).toBe("none");
  });

  test("a menu anchored inside a card keeps its blur too", async ({ page }) => {
    expect(await styleOf(page.getByTestId("menu-panel"), "backdrop-filter")).toBe("blur(40px)");
  });
});

test.describe("modal", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/design");
    await page.getByTestId("modal-trigger").click();
  });

  test("is a modal dialog to the browser", async ({ page }) => {
    const dialog = page.getByTestId("modal");

    await expect(dialog).toHaveRole("dialog");

    // Modality is asserted through the ::backdrop, not through an attribute.
    // `<dialog>` gets `aria-modal` implicitly and only `showModal()` renders a
    // backdrop — so a scrim that paints is proof the dialog was opened modally
    // and the page behind it is inert. An `aria-modal="true"` written by hand
    // would have asserted only that someone typed it.
    const scrim = await dialog.evaluate(
      (node) => getComputedStyle(node as Element, "::backdrop").backgroundColor,
    );

    expect(scrim).not.toBe("rgba(0, 0, 0, 0)");
  });

  test("takes the focus when it opens", async ({ page }) => {
    // Otherwise the keyboard stays behind the dialog, tabbing through a page
    // the user can no longer see.
    const focused = await page.evaluate(() =>
      document.activeElement?.closest("[data-testid='modal']") ? "inside" : "outside",
    );

    expect(focused).toBe("inside");
  });

  test("closes on Escape", async ({ page }) => {
    // Free with the native element, and the reason no dependency was added:
    // focus trap, Escape and the top layer all come from <dialog>.
    await page.keyboard.press("Escape");

    await expect(page.getByTestId("modal-panel")).toBeHidden();
  });
});

test.describe("menu", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/design");
  });

  test("announces itself as a menu", async ({ page }) => {
    await expect(page.getByTestId("menu-panel")).toHaveRole("menu");
    await expect(page.getByTestId("menu-item-versions")).toHaveRole("menuitem");
  });

  test("its items clear the touch target", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "the touch target is a phone concern");

    const box = await page.getByTestId("menu-item-versions").boundingBox();

    expect(box?.height).toBeGreaterThanOrEqual(44);
  });
});

test.describe("composer", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/design");
  });

  test("cannot send an empty message", async ({ page }) => {
    // Cheap to assert now, expensive to retrofit: an empty send is a webhook,
    // a job and an outbox row for nothing.
    await expect(page.getByTestId("composer-send")).toBeDisabled();

    await page.getByTestId("composer-input").fill("Oi");

    await expect(page.getByTestId("composer-send")).toBeEnabled();
  });

  test("is blocked while the AI is in control", async ({ page }) => {
    // Takeover is the product rule: while the agent answers, a human typing
    // into the same conversation is a race nobody wins.
    await expect(page.getByTestId("composer-blocked-input")).toBeDisabled();
  });
});

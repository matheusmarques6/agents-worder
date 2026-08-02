import { expect, test, type Locator } from "@playwright/test";

import { canonical } from "./support/css";

// E0-15 · L1 — action and input.
//
// The visual baseline says "it looks like the design". This file says the
// things a screenshot cannot: that the large button is actually 48px on a
// phone, that the toggle announces itself as a switch, that single choice is
// actually single. Those are the properties E5's screens and E5's axe run will
// depend on, and they are cheaper to fix now than after eleven screens consume
// them.

const SIZES = {
  sm: { height: "30px", radius: "9px" },
  md: { height: "38px", radius: "12px" },
  lg: { height: "48px", radius: "14px" },
};

async function styleOf(element: Locator, property: string): Promise<string> {
  return canonical(
    await element.evaluate(
      (node, name) => getComputedStyle(node as Element).getPropertyValue(name),
      property,
    ),
  );
}

async function boxOf(element: Locator) {
  const box = await element.boundingBox();
  if (!box) throw new Error("element is not rendered");
  return box;
}

test.describe("buttons", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/design");
  });

  for (const [size, expected] of Object.entries(SIZES)) {
    test(`size ${size} is ${expected.height} tall with the radius of its size`, async ({ page }) => {
      const button = page.getByTestId(`button-size-${size}`);

      expect((await boxOf(button)).height).toBe(Number.parseInt(expected.height, 10));
      expect(await styleOf(button, "border-radius")).toBe(expected.radius);
    });
  }

  test("the large size clears the 44px touch target", async ({ page }) => {
    // The design's own minimum, and the reason `lg` exists: on a phone every
    // primary action is lg. A control below 44 is one a thumb misses.
    const button = page.getByTestId("button-size-lg");

    expect((await boxOf(button)).height).toBeGreaterThanOrEqual(44);
  });

  test("a disabled button is disabled to the browser, not only to the eye", async ({ page }) => {
    const button = page.getByTestId("button-state-disabled");

    await expect(button).toBeDisabled();
  });

  test("a loading button stays disabled and says so", async ({ page }) => {
    // Otherwise a double click sends the request twice — and in this product a
    // duplicated request is a duplicated WhatsApp message.
    const button = page.getByTestId("button-state-loading");

    await expect(button).toBeDisabled();
    await expect(button).toHaveAttribute("aria-busy", "true");
  });
});

test.describe("fields", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/design");
  });

  test("a field in error is announced, not just coloured", async ({ page }) => {
    const input = page.getByTestId("field-error-input");

    await expect(input).toHaveAttribute("aria-invalid", "true");

    // The message has to be reachable from the field, or a screen reader
    // announces "invalid" and nothing else.
    const describedBy = await input.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    await expect(page.locator(`#${describedBy}`)).toHaveText(/dígitos/);
  });

  test("focus is visible", async ({ page }) => {
    // The one accessibility property that a design system either has from the
    // first component or never gets: every screen after this inherits it.
    const input = page.getByTestId("field-default-input");
    const resting = await styleOf(input, "border-color");

    await input.focus();

    expect(await styleOf(input, "border-color")).not.toBe(resting);
  });
});

test.describe("toggle", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/design");
  });

  test("is a switch and reports its state", async ({ page }) => {
    const toggle = page.getByTestId("toggle-followup");

    await expect(toggle).toHaveRole("switch");
    await expect(toggle).toHaveAttribute("aria-checked", "true");

    await toggle.click();

    await expect(toggle).toHaveAttribute("aria-checked", "false");
  });

  test("clears the touch target", async ({ page }) => {
    const toggle = page.getByTestId("toggle-followup");

    expect((await boxOf(toggle)).height).toBeGreaterThanOrEqual(44);
  });
});

test.describe("choice", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/design");
  });

  test("single choice is exclusive", async ({ page }) => {
    const friendly = page.getByTestId("choice-amigavel");
    const formal = page.getByTestId("choice-formal");

    await expect(friendly).toHaveAttribute("aria-checked", "true");

    await formal.click();

    await expect(formal).toHaveAttribute("aria-checked", "true");
    await expect(friendly).toHaveAttribute("aria-checked", "false");
  });

  test("chips accumulate", async ({ page }) => {
    // The opposite rule, one component away — which is exactly why both are
    // asserted: the difference is invisible in a screenshot.
    const tracking = page.getByTestId("chip-rastreio");
    const faq = page.getByTestId("chip-faq");

    await expect(tracking).toHaveAttribute("aria-pressed", "true");
    await expect(faq).toHaveAttribute("aria-pressed", "false");

    await faq.click();

    await expect(faq).toHaveAttribute("aria-pressed", "true");
    await expect(tracking).toHaveAttribute("aria-pressed", "true");
  });
});

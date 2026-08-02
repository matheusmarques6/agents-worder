import { expect, test, type Locator } from "@playwright/test";

import { canonical } from "./support/css";

// E0-15 · L4 — the conversation.
//
// This is the only surface where the product's actual content lives, and the
// only one a merchant will read hundreds of times a day. What is asserted here
// is what makes it readable at speed: which side a message came from, when it
// happened, and whether it arrived — each on its own channel, none of them
// colour alone.

async function styleOf(element: Locator, property: string): Promise<string> {
  return canonical(
    await element.evaluate(
      (node, name) => getComputedStyle(node as Element).getPropertyValue(name),
      property,
    ),
  );
}

test.describe("conversation bubbles", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/design");
  });

  test("what came in sits left, what went out sits right", async ({ page }) => {
    // Direction is the fastest read in a conversation, and it is carried by
    // position — which survives a screenshot in greyscale and a screen reader
    // reading in order.
    expect(await styleOf(page.getByTestId("bubble-inbound"), "align-self")).toBe("flex-start");
    expect(await styleOf(page.getByTestId("bubble-outbound"), "align-self")).toBe("flex-end");
  });

  test("the moment and the author are in mono", async ({ page }) => {
    // Princípio 03: times and identifiers line up in Geist Mono, so a column
    // of them can be scanned instead of read.
    const family = await styleOf(page.getByTestId("bubble-outbound-meta"), "font-family");

    expect(family).toContain("mono");
  });

  test("delivery status is readable, not only drawn", async ({ page }) => {
    // The double tick is an icon; on its own it tells a screen reader nothing.
    await expect(page.getByTestId("bubble-outbound-status")).toHaveAttribute(
      "aria-label",
      /lida/i,
    );
  });

  test("a human takeover is announced in the thread itself", async ({ page }) => {
    // The merchant scrolling back has to see where the agent stopped and a
    // person started, without cross-referencing another screen.
    await expect(page.getByTestId("thread-takeover")).toHaveText(/assumiu a conversa/i);
  });
});

import { expect, test, type Locator, type Page } from "@playwright/test";

import { canonical } from "./support/css";

// E0-15 · L2 — status and feedback.
//
// The rule this lot exists to protect is princípio 01: a semantic colour
// carries state and nothing else. The corollary is what these tests assert —
// colour is never the ONLY channel. Every status badge says its state in
// words, because eight percent of men cannot separate the green from the red,
// and because a screenshot of a colour is not an accessible interface.

const STATUSES = ["active", "paused", "onboarding", "shadow", "cancelled"];

const LABELS: Record<string, RegExp> = {
  active: /ativo/i,
  paused: /pausado/i,
  onboarding: /aprovação/i,
  shadow: /shadow/i,
  cancelled: /cancelado/i,
};

async function styleOf(element: Locator, property: string): Promise<string> {
  return canonical(
    await element.evaluate(
      (node, name) => getComputedStyle(node as Element).getPropertyValue(name),
      property,
    ),
  );
}

test.describe("status badges", () => {
  test.beforeEach(async ({ page }: { page: Page }) => {
    await page.goto("/design");
  });

  for (const status of STATUSES) {
    test(`${status} says its state in words, not only in colour`, async ({ page }) => {
      await expect(page.getByTestId(`status-${status}`)).toHaveText(LABELS[status]);
    });
  }

  test("no two statuses share a colour", async ({ page }) => {
    // Two states that look alike are two states nobody can tell apart at a
    // glance — which is the entire job of a status badge in a dashboard.
    const borders = await Promise.all(
      STATUSES.map((status) => styleOf(page.getByTestId(`status-${status}`), "border-color")),
    );

    expect(new Set(borders).size).toBe(STATUSES.length);
  });

  test("shadow is the only dashed one", async ({ page }) => {
    // The design draws it dashed because shadow mode is temporary — a border
    // that says "this is not the final state" without needing a legend.
    expect(await styleOf(page.getByTestId("status-shadow"), "border-style")).toBe("dashed");

    expect(await styleOf(page.getByTestId("status-active"), "border-style")).toBe("solid");
  });
});

test.describe("technical badges", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/design");
  });

  test("are typeset in the mono family", async ({ page }) => {
    // Princípio 03: values, IDs, deadlines and scores line up in Geist Mono.
    // An ID in a proportional face is an ID nobody can compare by eye.
    const family = await styleOf(page.getByTestId("tech-version"), "font-family");

    expect(family).toContain("geist");
    expect(family).toContain("mono");
  });
});

test.describe("alerts and toast", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/design");
  });

  test("an alert that demands attention announces itself as an alert", async ({ page }) => {
    // role=alert interrupts a screen reader; role=status waits its turn. The
    // difference is the difference between "your number is disconnected" and
    // "saved".
    await expect(page.getByTestId("alert-danger")).toHaveRole("alert");
    await expect(page.getByTestId("alert-warning")).toHaveRole("alert");
  });

  test("a toast reports rather than interrupts", async ({ page }) => {
    await expect(page.getByTestId("toast-success")).toHaveRole("status");
  });

  test("the toast is overlay glass and keeps it inside the showcase stage", async ({ page }) => {
    // Composition across lots: the toast is a Glass. If the stage it sits on
    // were itself glass, the nesting rule would strip this blur — and a toast
    // without its blur is a toast that stopped being an overlay.
    const toast = page.getByTestId("toast-success");

    expect(await styleOf(toast, "backdrop-filter")).toBe("blur(40px)");
  });
});

test.describe("skeleton and empty state", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/design");
  });

  test("a skeleton is hidden from assistive technology", async ({ page }) => {
    // It is a shape standing in for content that does not exist yet.
    // Announcing it reads as gibberish.
    await expect(page.getByTestId("skeleton-line")).toHaveAttribute("aria-hidden", "true");
  });

  test("an empty state can offer the way out, using the button from L1", async ({ page }) => {
    // First composition between lots: the empty state does not get a button of
    // its own. If it did, there would be two buttons in the system within a
    // week.
    const action = page.getByTestId("empty-action");

    await expect(action).toHaveAttribute("data-button", "");
    await expect(action).toBeEnabled();
  });
});
